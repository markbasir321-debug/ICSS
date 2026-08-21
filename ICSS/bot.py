import os
import io
import json
import html
import threading
import logging
from datetime import datetime, timezone

from flask import Flask

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from mcstatus import JavaServer

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import (
    MediaIoBaseUpload,
    MediaIoBaseDownload,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

MINECRAFT_IP = "194.93.2.184"
MINECRAFT_PORT = 25554

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения TELEGRAM_BOT_TOKEN"
    )


# ============================================================
# GOOGLE DRIVE
# ============================================================

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)

GOOGLE_DRIVE_FOLDER_ID = os.environ.get(
    "GOOGLE_DRIVE_FOLDER_ID"
)

GOOGLE_DRIVE_FILENAME = os.environ.get(
    "GOOGLE_DRIVE_FILENAME",
    "player_logs.json"
)

LOCAL_LOG_FILE = "player_logs.json"

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

drive_file_id = None


def get_drive_service():
    """
    Создаёт подключение к Google Drive.
    """

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON не задан."
        )
        return None

    if not GOOGLE_DRIVE_FOLDER_ID:
        logger.warning(
            "GOOGLE_DRIVE_FOLDER_ID не задан."
        )
        return None

    try:
        info = json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )

        credentials = (
            service_account.Credentials
            .from_service_account_info(
                info,
                scopes=DRIVE_SCOPES,
            )
        )

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        return service

    except Exception as e:
        logger.exception(
            "Ошибка подключения к Google Drive: %s",
            e,
        )

        return None


def save_logs_local():
    """
    Сохраняет лог локально.
    """

    try:
        with open(
            LOCAL_LOG_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                player_logs,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        logger.warning(
            "Не удалось сохранить локальный JSON: %s",
            e,
        )


def find_drive_file(service):
    """
    Ищет player_logs.json внутри указанной папки Google Drive.
    """

    query = (
        f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents "
        f"and name = '{GOOGLE_DRIVE_FILENAME}' "
        f"and trashed = false"
    )

    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id,name)",
            pageSize=10,
        )
        .execute()
    )

    files = result.get("files", [])

    if files:
        return files[0]["id"]

    return None


def sync_logs_to_drive():
    """
    Загружает player_logs.json на Google Drive.
    Если файл уже существует — обновляет его.
    Если нет — создаёт.
    """

    global drive_file_id

    service = get_drive_service()

    if service is None:
        return

    try:

        data = json.dumps(
            player_logs,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype="application/json",
            resumable=False,
        )

        if drive_file_id is None:
            drive_file_id = find_drive_file(service)

        if drive_file_id:

            service.files().update(
                fileId=drive_file_id,
                media_body=media,
                fields="id",
            ).execute()

            logger.info(
                "player_logs.json обновлён на Google Drive"
            )

        else:

            metadata = {
                "name": GOOGLE_DRIVE_FILENAME,
                "parents": [
                    GOOGLE_DRIVE_FOLDER_ID
                ],
            }

            created = (
                service.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id",
                )
                .execute()
            )

            drive_file_id = created["id"]

            logger.info(
                "player_logs.json создан на Google Drive"
            )

    except Exception as e:

        logger.exception(
            "Ошибка синхронизации с Google Drive: %s",
            e,
        )


def load_logs_from_drive():
    """
    Загружает существующий player_logs.json
    с Google Drive при запуске.
    """

    global drive_file_id
    global player_logs

    service = get_drive_service()

    if service is None:
        return

    try:

        drive_file_id = find_drive_file(service)

        if not drive_file_id:

            logger.info(
                "player_logs.json на Google Drive ещё нет."
            )

            return

        request = service.files().get_media(
            fileId=drive_file_id
        )

        buffer = io.BytesIO()

        downloader = MediaIoBaseDownload(
            buffer,
            request,
        )

        done = False

        while not done:

            _, done = downloader.next_chunk()

        raw = buffer.getvalue()

        loaded = json.loads(
            raw.decode("utf-8")
        )

        if isinstance(loaded, list):

            player_logs = loaded[
                -MAX_PLAYER_LOGS:
            ]

            save_logs_local()

            logger.info(
                "Загружено %s событий из Google Drive",
                len(player_logs),
            )

    except Exception as e:

        logger.exception(
            "Ошибка загрузки логов с Google Drive: %s",
            e,
        )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(name)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# ДАННЫЕ
# ============================================================

targets = {}

TARGET_CHECK_INTERVAL = 10

player_logs = []

MAX_PLAYER_LOGS = 200

previous_players = None

log_subscribers = set()


# ============================================================
# FLASK
# ============================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():

    return (
        "Minecraft Telegram Bot is running",
        200,
    )


@web_app.route("/health")
def health():

    return "OK", 200


def run_web():

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    web_app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# MINECRAFT
# ============================================================

def get_server_status():

    try:

        address = (
            f"{MINECRAFT_IP}:"
            f"{MINECRAFT_PORT}"
        )

        server = JavaServer.lookup(
            address
        )

        status = server.status()

        players_online = (
            status.players.online
        )

        players_max = (
            status.players.max
        )

        players = []

        # ВАЖНО:
        # sample может содержать НЕ всех игроков.
        # Поэтому этот список нельзя считать полным.

        if status.players.sample:

            for player in status.players.sample:

                if player.name:

                    players.append(
                        player.name
                    )

        return {
            "online": True,
            "players_online": players_online,
            "players_max": players_max,
            "players": players,
            "latency": round(
                status.latency
            ),
            "sample_complete": (
                len(players)
                >= players_online
            ),
        }

    except Exception as e:

        logger.warning(
            "Ошибка проверки Minecraft: %s",
            e,
        )

        return {
            "online": False,
            "players_online": 0,
            "players_max": 0,
            "players": [],
            "latency": None,
            "sample_complete": False,
        }


# ============================================================
# HTML ESCAPE
# ============================================================

def safe_html(text):
    return html.escape(
        str(text),
        quote=False,
    )


# ============================================================
# STATUS
# ============================================================

def make_status_text():

    data = get_server_status()

    if not data["online"]:

        return (
            "🔴 <b>Сервер OFFLINE</b>\n\n"
            f"🌐 "
            f"<code>"
            f"{safe_html(MINECRAFT_IP)}:"
            f"{MINECRAFT_PORT}"
            f"</code>"
        )

    text = (
        "🟢 <b>Сервер ONLINE</b>\n\n"
        f"🌐 "
        f"<code>"
        f"{safe_html(MINECRAFT_IP)}:"
        f"{MINECRAFT_PORT}"
        f"</code>\n"
        f"👥 Игроков: "
        f"<b>"
        f"{data['players_online']}/"
        f"{data['players_max']}"
        f"</b>\n"
        f"📡 Ping: "
        f"<b>{data['latency']} ms</b>"
    )

    if data["players"]:

        text += (
            "\n\n"
            "👤 <b>Игроки:</b>\n"
        )

        for player in data["players"]:

            text += (
                f"• "
                f"{safe_html(player)}\n"
            )

        if not data["sample_complete"]:

            text += (
                "\n⚠️ "
                "<i>Сервер предоставил "
                "неполный список игроков.</i>"
            )

    elif data["players_online"] > 0:

        text += (
            "\n\n"
            "⚠️ "
            "Сервер не предоставил "
            "список игроков."
        )

    return text


# ============================================================
# FIND PLAYER
# ============================================================

def find_player(
    players,
    target_name,
):

    target = target_name.casefold()

    for player in players:

        if player.casefold() == target:

            return player

    return None


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🤖 <b>Minecraft Server Bot</b>\n\n"

        "Команды:\n"

        "/status — состояние сервера\n"
        "/online — количество игроков\n"
        "/players — список игроков\n\n"

        "/target &lt;ник&gt; — "
        "отслеживать игрока\n"

        "/untarget — "
        "перестать отслеживать\n\n"

        "/log — "
        "включить/выключить общий лог\n"

        "/logs — "
        "последние события\n"

        "/logger — "
        "лог игроков\n"

        "/logger &lt;ник&gt; — "
        "история конкретного игрока\n"

        "/ap — "
        "игроки, встречавшиеся в истории",

        parse_mode="HTML",
    )


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        make_status_text(),
        parse_mode="HTML",
    )


# ============================================================
# /ONLINE
# ============================================================

async def online_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = get_server_status()

    if not data["online"]:

        await update.message.reply_text(
            "🔴 Сервер OFFLINE"
        )

        return

    await update.message.reply_text(
        "🟢 Сервер ONLINE\n\n"
        f"👥 Игроков: "
        f"{data['players_online']}/"
        f"{data['players_max']}"
    )


# ============================================================
# /PLAYERS
# ============================================================

async def players_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = get_server_status()

    if not data["online"]:

        await update.message.reply_text(
            "🔴 Сервер OFFLINE"
        )

        return

    if not data["players"]:

        await update.message.reply_text(
            "🟢 Сервер ONLINE\n"
            f"👥 Игроков: "
            f"{data['players_online']}/"
            f"{data['players_max']}\n\n"
            "⚠️ Список игроков "
            "сервер не предоставил."
        )

        return

    text = (
        f"🟢 Игроков онлайн: "
        f"{data['players_online']}/"
        f"{data['players_max']}\n\n"

        "👤 <b>Игроки:</b>\n"
    )

    for player in data["players"]:

        text += (
            f"• "
            f"{safe_html(player)}\n"
        )

    if not data["sample_complete"]:

        text += (
            "\n⚠️ "
            "<i>Это неполный список. "
            "Сервер передал только "
            f"{len(data['players'])} "
            "ников.</i>"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# /TARGET
# ============================================================

async def target_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "Использование:\n"
            "/target <ник>\n\n"
            "После этого бот будет "
            "сообщать о входе и выходе."
        )

        return

    target_name = " ".join(
        context.args
    ).strip()

    data = get_server_status()

    if not data["online"]:

        last_seen = None

        status_text = (
            "🔴 Сервер сейчас OFFLINE.\n"
            "Состояние игрока "
            "определю при следующей "
            "проверке."
        )

    elif not data["players"]:

        last_seen = None

        status_text = (
            "⚠️ Сервер не предоставил "
            "список игроков.\n"
            "Состояние игрока "
            "определю позже."
        )

    else:

        found = find_player(
            data["players"],
            target_name,
        )

        last_seen = (
            found is not None
        )

        if found:

            status_text = (
                "🟢 <b>"
                f"{safe_html(found)}"
                "</b> сейчас онлайн."
            )

        else:

            status_text = (
                "🔴 <b>"
                f"{safe_html(target_name)}"
                "</b> сейчас не найден."
            )

    targets[chat_id] = {
        "name": target_name,
        "last_seen": last_seen,
    }

    await update.message.reply_text(
        "🎯 Теперь отслеживаю "
        f"<b>{safe_html(target_name)}</b>.\n\n"
        f"{status_text}\n\n"
        f"🔄 Проверка каждые "
        f"{TARGET_CHECK_INTERVAL} секунд.",

        parse_mode="HTML",
    )


# ============================================================
# /UNTARGET
# ============================================================

async def untarget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if chat_id not in targets:

        await update.message.reply_text(
            "Сейчас никто не отслеживается."
        )

        return

    name = targets[chat_id]["name"]

    del targets[chat_id]

    await update.message.reply_text(
        "🛑 Перестал отслеживать "
        f"<b>{safe_html(name)}</b>.",
        parse_mode="HTML",
    )


# ============================================================
# PLAYER LOG
# ============================================================

def add_player_log(
    event,
    player,
):

    entry = {
        "time": datetime.now(
            timezone.utc
        ).astimezone().strftime(
            "%d.%m.%Y %H:%M:%S"
        ),

        "event": event,

        "player": player,
    }

    player_logs.append(entry)

    if len(player_logs) > MAX_PLAYER_LOGS:

        del player_logs[
            :-MAX_PLAYER_LOGS
        ]

    save_logs_local()

    # Синхронизируем каждый новый event.
    sync_logs_to_drive()

    logger.info(
        "PLAYER %s: %s",
        event.upper(),
        player,
    )


# ============================================================
# /LOG
# ============================================================

async def log_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = update.effective_chat.id

    if chat_id in log_subscribers:

        log_subscribers.remove(
            chat_id
        )

        await update.message.reply_text(
            "🛑 Общий лог игроков "
            "выключен."
        )

        return

    log_subscribers.add(chat_id)

    await update.message.reply_text(
        "📋 Общий лог игроков включён.\n\n"
        "Бот будет сообщать сюда "
        "о входах и выходах игроков."
    )


# ============================================================
# /LOGS
# ============================================================

async def logs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not player_logs:

        await update.message.reply_text(
            "📋 Лог пока пуст."
        )

        return

    limit = 20

    if context.args:

        try:

            limit = max(
                1,
                min(
                    int(context.args[0]),
                    50,
                ),
            )

        except ValueError:

            pass

    entries = player_logs[
        -limit:
    ]

    lines = [
        "📋 <b>Лог игроков</b>\n"
    ]

    for entry in entries:

        if entry["event"] == "join":

            icon = "🟢"
            action = "зашёл"

        else:

            icon = "🔴"
            action = "вышел"

        lines.append(
            f"{icon} "
            f"<code>"
            f"{safe_html(entry['time'])}"
            f"</code> — "
            f"<b>"
            f"{safe_html(entry['player'])}"
            f"</b> "
            f"{action}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# /LOGGER
# ============================================================

async def logger_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:

        await logs_command(
            update,
            context,
        )

        return

    target = " ".join(
        context.args
    ).casefold()

    matches = [
        entry
        for entry in player_logs
        if entry["player"].casefold()
        == target
    ]

    if not matches:

        await update.message.reply_text(
            "📋 Для этого игрока "
            "записей пока нет."
        )

        return

    player_name = matches[
        -1
    ]["player"]

    lines = [
        "📋 <b>История "
        f"{safe_html(player_name)}"
        "</b>\n"
    ]

    for entry in matches[-50:]:

        if entry["event"] == "join":

            icon = "🟢"
            action = "зашёл"

        else:

            icon = "🔴"
            action = "вышел"

        lines.append(
            f"{icon} "
            f"<code>"
            f"{safe_html(entry['time'])}"
            f"</code> — "
            f"{action}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# /AP
# ============================================================

async def ap_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not player_logs:

        await update.message.reply_text(
            "📋 История игроков пока пуста."
        )

        return

    names = {}

    for entry in player_logs:

        names[
            entry["player"].casefold()
        ] = entry["player"]

    lines = [
        "👤 <b>Игроки из истории:</b>"
    ]

    for name in sorted(
        names.values(),
        key=str.casefold,
    ):

        lines.append(
            f"• {safe_html(name)}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# AUTOMATIC CHECK
# ============================================================

async def automatic_check(
    context: ContextTypes.DEFAULT_TYPE,
):

    global previous_players

    data = get_server_status()

    if not data["online"]:

        logger.info(
            "Minecraft OFFLINE"
        )

        # НИКОГДА не считаем всех
        # игроков вышедшими при OFFLINE.
        return

    logger.info(
        "Minecraft ONLINE — %s/%s игроков",
        data["players_online"],
        data["players_max"],
    )

    # --------------------------------------------------------
    # ВАЖНО:
    #
    # Если sample неполный, нельзя сравнивать его
    # с предыдущим sample и делать JOIN/LEAVE.
    #
    # Иначе бот будет писать ложные события.
    # --------------------------------------------------------

    if not data["players"]:

        return

    current_players = {
        player.casefold(): player
        for player in data["players"]
    }

    sample_complete = data[
        "sample_complete"
    ]

    if (
        previous_players is None
        or not sample_complete
    ):

        # Запоминаем только если sample
        # можно считать полным.
        if sample_complete:

            previous_players = (
                current_players
            )

        # /target отдельно всё равно
        # может работать только если
        # искомый игрок присутствует.
        await check_targets(
            context,
            data,
        )

        return

    joined_keys = (
        set(current_players)
        - set(previous_players)
    )

    left_keys = (
        set(previous_players)
        - set(current_players)
    )

    # --------------------------------------------------------
    # JOIN
    # --------------------------------------------------------

    for key in joined_keys:

        player = current_players[key]

        add_player_log(
            "join",
            player,
        )

        for chat_id in list(
            log_subscribers
        ):

            try:

                await context.bot.send_message(
                    chat_id=chat_id,

                    text=(
                        "🟢 <b>"
                        f"{safe_html(player)}"
                        "</b> зашёл "
                        "на сервер!"
                    ),

                    parse_mode="HTML",
                )

            except Exception as e:

                logger.warning(
                    "Не удалось отправить "
                    "JOIN в чат %s: %s",
                    chat_id,
                    e,
                )

    # --------------------------------------------------------
    # LEAVE
    # --------------------------------------------------------

    for key in left_keys:

        player = previous_players[
            key
        ]

        add_player_log(
            "leave",
            player,
        )

        for chat_id in list(
            log_subscribers
        ):

            try:

                await context.bot.send_message(
                    chat_id=chat_id,

                    text=(
                        "🔴 <b>"
                        f"{safe_html(player)}"
                        "</b> вышел "
                        "с сервера!"
                    ),

                    parse_mode="HTML",
                )

            except Exception as e:

                logger.warning(
                    "Не удалось отправить "
                    "LEAVE в чат %s: %s",
                    chat_id,
                    e,
                )

    previous_players = (
        current_players
    )

    await check_targets(
        context,
        data,
    )


# ============================================================
# TARGET CHECK
# ============================================================

async def check_targets(
    context,
    data,
):

    if not targets:

        return

    for chat_id, target in list(
        targets.items()
    ):

        target_name = target[
            "name"
        ]

        previous = target[
            "last_seen"
        ]

        found = find_player(
            data["players"],
            target_name,
        )

        current = (
            found is not None
        )

        # Если sample неполный,
        # отсутствие игрока НЕ означает,
        # что он вышел.
        if not data[
            "sample_complete"
        ]:

            if found:

                target[
                    "last_seen"
                ] = True

            continue

        if previous is None:

            target[
                "last_seen"
            ] = current

            continue

        if current == previous:

            continue

        target[
            "last_seen"
        ] = current

        try:

            if current:

                await context.bot.send_message(
                    chat_id=chat_id,

                    text=(
                        "🟢 <b>"
                        f"{safe_html(target_name)}"
                        "</b> зашёл "
                        "на сервер!"
                    ),

                    parse_mode="HTML",
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,

                    text=(
                        "🔴 <b>"
                        f"{safe_html(target_name)}"
                        "</b> вышел "
                        "с сервера!"
                    ),

                    parse_mode="HTML",
                )

        except Exception as e:

            logger.warning(
                "Не удалось отправить "
                "target в чат %s: %s",
                chat_id,
                e,
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Ошибка Telegram:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Flask
    # --------------------------------------------------------

    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

    # --------------------------------------------------------
    # Google Drive
    # --------------------------------------------------------

    load_logs_from_drive()

    logger.info(
        "Запуск Telegram-бота..."
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "online",
            online_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "players",
            players_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "target",
            target_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "untarget",
            untarget_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "log",
            log_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "logs",
            logs_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "logger",
            logger_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ap",
            ap_command,
        )
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Minecraft checker
    # --------------------------------------------------------

    application.job_queue.run_repeating(
        automatic_check,
        interval=TARGET_CHECK_INTERVAL,
        first=10,
    )

    logger.info(
        "Telegram-бот запущен!"
    )

    # --------------------------------------------------------
    # Polling
    # --------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
