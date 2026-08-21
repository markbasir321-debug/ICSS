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
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload


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


# ============================================================
# ОБЩИЕ НАСТРОЙКИ
# ============================================================

CHECK_INTERVAL = 10

MAX_PLAYER_LOGS = 5000


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# ДАННЫЕ
# ============================================================

# Все игроки, которых бот когда-либо обнаружил.
known_players = {}

# История входов/выходов.
player_logs = []

# Последний известный список игроков.
previous_players = None

# Игроки, отслеживаемые через /target.
targets = {}

# Чаты, куда отправляется общий лог.
log_subscribers = set()

# ID файла на Google Drive.
drive_file_id = None


# ============================================================
# GOOGLE DRIVE SERVICE
# ============================================================

def get_drive_service():
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
            service_account
            .Credentials
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


# ============================================================
# СОХРАНЕНИЕ ЛОКАЛЬНО
# ============================================================

def save_logs_local():
    try:
        data = {
            "known_players": sorted(
                known_players.values(),
                key=str.casefold,
            ),
            "events": player_logs,
        }

        with open(
            LOCAL_LOG_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        logger.info(
            "Логи сохранены локально: %s",
            LOCAL_LOG_FILE,
        )

    except Exception as e:
        logger.exception(
            "Ошибка локального сохранения: %s",
            e,
        )


# ============================================================
# GOOGLE DRIVE - ПОИСК ФАЙЛА
# ============================================================

def find_drive_file(service):
    global drive_file_id

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

    files = result.get(
        "files",
        [],
    )

    if files:
        drive_file_id = files[0]["id"]
        return drive_file_id

    return None


# ============================================================
# ЗАГРУЗКА С GOOGLE DRIVE
# ============================================================

def load_logs_from_drive():
    global drive_file_id
    global player_logs
    global known_players

    service = get_drive_service()

    if service is None:
        logger.warning(
            "Google Drive недоступен. Используется локальный файл."
        )

        load_logs_local()

        return

    try:
        file_id = find_drive_file(service)

        if not file_id:
            logger.info(
                "player_logs.json на Google Drive ещё нет."
            )

            load_logs_local()

            return

        request = (
            service.files()
            .get_media(
                fileId=file_id
            )
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

        if not raw:
            logger.warning(
                "Google Drive вернул пустой файл."
            )

            return

        data = json.loads(
            raw.decode("utf-8")
        )

        # Новый формат
        if isinstance(data, dict):

            loaded_players = data.get(
                "known_players",
                [],
            )

            loaded_events = data.get(
                "events",
                [],
            )

            if isinstance(
                loaded_players,
                list,
            ):
                known_players = {}

                for player in loaded_players:
                    if isinstance(
                        player,
                        str,
                    ):
                        known_players[
                            player.casefold()
                        ] = player

            if isinstance(
                loaded_events,
                list,
            ):
                player_logs = (
                    loaded_events[
                        -MAX_PLAYER_LOGS:
                    ]
                )

        # Старый формат — просто список событий
        elif isinstance(data, list):

            player_logs = (
                data[
                    -MAX_PLAYER_LOGS:
                ]
            )

            known_players = {}

            for event in player_logs:

                if not isinstance(
                    event,
                    dict,
                ):
                    continue

                player = event.get(
                    "player"
                )

                if isinstance(
                    player,
                    str,
                ):
                    known_players[
                        player.casefold()
                    ] = player

        save_logs_local()

        logger.info(
            "Google Drive: загружено игроков: %s, событий: %s",
            len(known_players),
            len(player_logs),
        )

    except Exception as e:
        logger.exception(
            "Ошибка загрузки Google Drive: %s",
            e,
        )

        load_logs_local()


# ============================================================
# ЗАГРУЗКА ЛОКАЛЬНОГО ФАЙЛА
# ============================================================

def load_logs_local():

    global player_logs
    global known_players

    if not os.path.exists(
        LOCAL_LOG_FILE
    ):
        logger.info(
            "Локального player_logs.json нет."
        )

        save_logs_local()

        return

    try:

        with open(
            LOCAL_LOG_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            dict,
        ):

            players = data.get(
                "known_players",
                [],
            )

            events = data.get(
                "events",
                [],
            )

            known_players = {}

            for player in players:

                if isinstance(
                    player,
                    str,
                ):
                    known_players[
                        player.casefold()
                    ] = player

            if isinstance(
                events,
                list,
            ):
                player_logs = (
                    events[
                        -MAX_PLAYER_LOGS:
                    ]
                )

        elif isinstance(
            data,
            list,
        ):

            player_logs = (
                data[
                    -MAX_PLAYER_LOGS:
                ]
            )

            known_players = {}

            for event in player_logs:

                if not isinstance(
                    event,
                    dict,
                ):
                    continue

                player = event.get(
                    "player"
                )

                if isinstance(
                    player,
                    str,
                ):
                    known_players[
                        player.casefold()
                    ] = player

        logger.info(
            "Локально загружено игроков: %s, событий: %s",
            len(known_players),
            len(player_logs),
        )

    except Exception as e:
        logger.exception(
            "Ошибка чтения player_logs.json: %s",
            e,
        )


# ============================================================
# СОХРАНЕНИЕ НА GOOGLE DRIVE
# ============================================================

def sync_logs_to_drive():

    global drive_file_id

    service = get_drive_service()

    if service is None:
        return

    try:

        data = {
            "known_players": sorted(
                known_players.values(),
                key=str.casefold,
            ),
            "events": player_logs,
        }

        raw = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        media = MediaIoBaseUpload(
            io.BytesIO(raw),
            mimetype="application/json",
            resumable=False,
        )

        if drive_file_id is None:

            drive_file_id = find_drive_file(
                service
            )

        if drive_file_id:

            service.files().update(
                fileId=drive_file_id,
                media_body=media,
                fields="id",
            ).execute()

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
            "player_logs.json синхронизирован с Google Drive."
        )

    except Exception as e:
        logger.exception(
            "Ошибка синхронизации Google Drive: %s",
            e,
        )


# ============================================================
# ДОБАВЛЕНИЕ ИГРОКА В СПИСОК
# ============================================================

def add_known_player(player):

    if not player:
        return False

    key = player.casefold()

    if key in known_players:
        return False

    known_players[key] = player

    logger.info(
        "Новый игрок добавлен в known_players: %s",
        player,
    )

    save_logs_local()
    sync_logs_to_drive()

    return True


# ============================================================
# ДОБАВЛЕНИЕ СОБЫТИЯ
# ============================================================

def add_player_log(
    event,
    player,
):

    if not player:
        return

    add_known_player(
        player
    )

    entry = {
        "time": (
            datetime
            .now(timezone.utc)
            .astimezone()
            .strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        ),
        "event": event,
        "player": player,
    }

    player_logs.append(
        entry
    )

    if len(player_logs) > MAX_PLAYER_LOGS:

        del player_logs[
            :-MAX_PLAYER_LOGS
        ]

    save_logs_local()
    sync_logs_to_drive()

    logger.info(
        "PLAYER %s: %s",
        event.upper(),
        player,
    )


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

    return (
        "OK",
        200,
    )


def run_web():

    port = int(
        os.environ.get(
            "PORT",
            "8000",
        )
    )

    web_app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# MINECRAFT STATUS
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

        if status.players.sample:

            for player in (
                status.players.sample
            ):

                if player.name:

                    players.append(
                        player.name
                    )

        # Убираем дубли.
        unique_players = {}

        for player in players:

            unique_players[
                player.casefold()
            ] = player

        players = list(
            unique_players.values()
        )

        return {
            "online": True,
            "players_online": players_online,
            "players_max": players_max,
            "players": players,
            "latency": round(
                status.latency
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
        }


# ============================================================
# ПОИСК ИГРОКА
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
# HTML SAFE
# ============================================================

def safe(text):

    return html.escape(
        str(text)
    )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🤖 <b>Minecraft Server Bot</b>\n\n"
        "/status — состояние сервера\n"
        "/online — количество игроков\n"
        "/players — текущий список игроков\n"
        "/target &lt;ник&gt; — отслеживать игрока\n"
        "/untarget — прекратить отслеживание\n"
        "/log — включить/выключить общий лог\n"
        "/logs — последние события\n"
        "/logger — история игроков\n"
        "/logger &lt;ник&gt; — история конкретного игрока\n"
        "/ap — все обнаруженные игроки",
        parse_mode="HTML",
    )


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    data = get_server_status()

    if not data["online"]:

        await update.message.reply_text(
            "🔴 <b>Сервер OFFLINE</b>",
            parse_mode="HTML",
        )

        return

    text = (
        "🟢 <b>Сервер ONLINE</b>\n\n"
        f"🌐 <code>{safe(MINECRAFT_IP)}:"
        f"{MINECRAFT_PORT}</code>\n"
        f"👥 Игроков: "
        f"<b>{data['players_online']}/"
        f"{data['players_max']}</b>\n"
        f"📡 Ping: "
        f"<b>{data['latency']} ms</b>"
    )

    if data["players"]:

        text += (
            "\n\n👤 <b>Полученный список:</b>\n"
        )

        for player in data["players"]:

            text += (
                f"• {safe(player)}\n"
            )

    if (
        data["players_online"]
        > len(data["players"])
    ):

        text += (
            "\n⚠️ Minecraft Status "
            "предоставил не полный sample "
            "игроков."
        )

    await update.message.reply_text(
        text,
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
        f"🟢 Сервер ONLINE\n\n"
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
            f"🟢 Сервер ONLINE\n"
            f"👥 Игроков: "
            f"{data['players_online']}/"
            f"{data['players_max']}\n\n"
            "Список игроков сервер не предоставил."
        )

        return

    text = (
        f"🟢 Игроков онлайн: "
        f"{data['players_online']}/"
        f"{data['players_max']}\n\n"
        "👤 <b>Полученный список:</b>\n"
    )

    for player in data["players"]:

        text += (
            f"• {safe(player)}\n"
        )

    if (
        data["players_online"]
        > len(data["players"])
    ):

        text += (
            "\n⚠️ Важно: сервер сообщил "
            f"{data['players_online']} игроков, "
            f"но через Status API получено "
            f"только {len(data['players'])} ников."
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

    chat_id = (
        update.effective_chat.id
    )

    if not context.args:

        await update.message.reply_text(
            "Использование:\n"
            "/target <ник>"
        )

        return

    target_name = (
        " ".join(
            context.args
        )
        .strip()
    )

    data = get_server_status()

    if not data["online"]:

        last_seen = None

        status_text = (
            "🔴 Сервер сейчас OFFLINE."
        )

    elif not data["players"]:

        last_seen = None

        status_text = (
            "⚠️ Список игроков сейчас "
            "не предоставлен."
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
                f"🟢 <b>{safe(found)}</b> "
                "сейчас онлайн."
            )

        else:

            status_text = (
                f"🔴 <b>{safe(target_name)}</b> "
                "сейчас не на сервере."
            )

    targets[chat_id] = {
        "name": target_name,
        "last_seen": last_seen,
    }

    await update.message.reply_text(
        f"🎯 Теперь отслеживаю "
        f"<b>{safe(target_name)}</b>.\n\n"
        f"{status_text}\n\n"
        f"🔄 Проверка каждые "
        f"{CHECK_INTERVAL} секунд.",
        parse_mode="HTML",
    )


# ============================================================
# /UNTARGET
# ============================================================

async def untarget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )

    if chat_id not in targets:

        await update.message.reply_text(
            "Сейчас никто не отслеживается."
        )

        return

    name = targets[
        chat_id
    ]["name"]

    del targets[
        chat_id
    ]

    await update.message.reply_text(
        f"🛑 Перестал отслеживать "
        f"<b>{safe(name)}</b>.",
        parse_mode="HTML",
    )


# ============================================================
# /LOG
# ============================================================

async def log_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat_id = (
        update.effective_chat.id
    )

    if chat_id in log_subscribers:

        log_subscribers.remove(
            chat_id
        )

        await update.message.reply_text(
            "🛑 Общий лог выключен."
        )

        return

    log_subscribers.add(
        chat_id
    )

    await update.message.reply_text(
        "📋 Общий лог включён.\n\n"
        "Бот будет отправлять сообщения "
        "о новых входах и выходах."
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
            "📋 История пока пустая.\n\n"
            f"Известных игроков: "
            f"{len(known_players)}"
        )

        return

    limit = 20

    if context.args:

        try:

            limit = max(
                1,
                min(
                    int(
                        context.args[0]
                    ),
                    50,
                ),
            )

        except ValueError:
            pass

    entries = (
        player_logs[
            -limit:
        ]
    )

    lines = [
        "📋 <b>Последние события</b>\n"
    ]

    for entry in entries:

        if entry.get(
            "event"
        ) == "join":

            icon = "🟢"
            action = "зашёл"

        else:

            icon = "🔴"
            action = "вышел"

        lines.append(
            f"{icon} "
            f"<code>{safe(entry.get('time', ''))}</code> — "
            f"<b>{safe(entry.get('player', ''))}</b> "
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

    if context.args:

        target = (
            " ".join(
                context.args
            )
            .casefold()
        )

        matches = [
            event
            for event in player_logs
            if str(
                event.get(
                    "player",
                    ""
                )
            ).casefold()
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
        ].get(
            "player",
            target,
        )

        lines = [
            f"📋 <b>История "
            f"{safe(player_name)}</b>\n"
        ]

        for event in matches[
            -50:
        ]:

            if event.get(
                "event"
            ) == "join":

                icon = "🟢"
                action = "зашёл"

            else:

                icon = "🔴"
                action = "вышел"

            lines.append(
                f"{icon} "
                f"<code>{safe(event.get('time', ''))}</code> — "
                f"{action}"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
        )

        return

    await logs_command(
        update,
        context,
    )


# ============================================================
# /AP
# ============================================================

async def ap_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not known_players:

        await update.message.reply_text(
            "📋 Список игроков пока пуст.\n"
            "Бот добавит игрока автоматически, "
            "как только получит его от сервера."
        )

        return

    players = sorted(
        known_players.values(),
        key=str.casefold,
    )

    lines = [
        "👤 <b>Все обнаруженные игроки:</b>\n"
    ]

    for player in players:

        lines.append(
            f"• {safe(player)}"
        )

    text = "\n".join(
        lines
    )

    # Telegram имеет ограничение на размер сообщения.
    if len(text) > 4000:

        text = (
            "\n".join(
                lines[:300]
            )
            + "\n\n⚠️ Список слишком большой."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# АВТОМАТИЧЕСКАЯ ПРОВЕРКА
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

        return

    logger.info(
        "Minecraft ONLINE — %s/%s игроков; "
        "получено ников: %s",
        data["players_online"],
        data["players_max"],
        len(data["players"]),
    )

    # --------------------------------------------------------
    # Каждый опрос обрабатываем заново.
    # --------------------------------------------------------

    current_players = {}

    for player in data["players"]:

        current_players[
            player.casefold()
        ] = player

        # Автоматически добавляем
        # каждого нового обнаруженного игрока.
        add_known_player(
            player
        )

    # --------------------------------------------------------
    # Если список вообще отсутствует,
    # нельзя определять leave.
    # --------------------------------------------------------

    if not current_players:

        logger.warning(
            "Minecraft не предоставил список игроков."
        )

        return

    # --------------------------------------------------------
    # Первый нормальный опрос.
    # --------------------------------------------------------

    if previous_players is None:

        previous_players = (
            current_players
        )

        logger.info(
            "Начальный список игроков сохранён."
        )

        return

    # --------------------------------------------------------
    # JOIN
    # --------------------------------------------------------

    joined = (
        set(current_players)
        - set(previous_players)
    )

    # --------------------------------------------------------
    # LEAVE
    # --------------------------------------------------------

    left = (
        set(previous_players)
        - set(current_players)
    )

    # --------------------------------------------------------
    # Обрабатываем входы.
    # --------------------------------------------------------

    for key in joined:

        player = current_players[
            key
        ]

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
                        f"🟢 <b>{safe(player)}</b> "
                        "зашёл на сервер!"
                    ),
                    parse_mode="HTML",
                )

            except Exception as e:

                logger.warning(
                    "Ошибка отправки JOIN: %s",
                    e,
                )

    # --------------------------------------------------------
    # Обрабатываем выходы.
    # --------------------------------------------------------

    for key in left:

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
                        f"🔴 <b>{safe(player)}</b> "
                        "вышел с сервера!"
                    ),
                    parse_mode="HTML",
                )

            except Exception as e:

                logger.warning(
                    "Ошибка отправки LEAVE: %s",
                    e,
                )

    # --------------------------------------------------------
    # Запоминаем новый список.
    # --------------------------------------------------------

    previous_players = (
        current_players
    )

    # --------------------------------------------------------
    # TARGET
    # --------------------------------------------------------

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
                        f"🟢 <b>{safe(target_name)}</b> "
                        "зашёл на сервер!"
                    ),
                    parse_mode="HTML",
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔴 <b>{safe(target_name)}</b> "
                        "вышел с сервера!"
                    ),
                    parse_mode="HTML",
                )

        except Exception as e:

            logger.warning(
                "Ошибка TARGET: %s",
                e,
            )


# ============================================================
# MAIN
# ============================================================

def main():

    # Создаём JSON сразу,
    # даже если Google Drive пока недоступен.
    save_logs_local()

    # Загружаем старые данные.
    load_logs_from_drive()

    # Ещё раз сохраняем после загрузки,
    # чтобы локальный файл точно существовал.
    save_logs_local()

    # Flask для Render.
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

    logger.info(
        "Запуск Telegram-бота..."
    )

    application = (
        Application
        .builder()
        .token(
            TELEGRAM_TOKEN
        )
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
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
    # ПРОВЕРКА КАЖДЫЕ 10 СЕКУНД
    # --------------------------------------------------------

    application.job_queue.run_repeating(
        automatic_check,
        interval=CHECK_INTERVAL,
        first=5,
    )

    logger.info(
        "Telegram-бот запущен!"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
