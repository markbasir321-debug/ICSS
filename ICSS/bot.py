import os
import io
import json
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

CHECK_INTERVAL = 10

MAX_PLAYER_LOGS = 5000


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_BOT_TOKEN"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - "
        "%(name)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
)

logger = logging.getLogger(__name__)


# ============================================================
# ДАННЫЕ
# ============================================================

# История входов/выходов.
player_logs = []

# Все игроки, которых бот когда-либо видел.
known_players = {}

# Последний полный список игроков.
previous_players = None

# Подписчики на автоматические уведомления.
log_subscribers = set()

# Игроки, отслеживаемые через /target.
targets = {}

# ID файла на Google Drive.
drive_file_id = None

# Блокировка Google Drive.
drive_lock = threading.Lock()


# ============================================================
# IGNORE
# ============================================================

def is_ignored_player(name):
    if not name:
        return True

    name = str(name).strip()

    ignored = {
        "anonymous player",
        "anonymous_player",
    }

    return name.casefold() in ignored


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON не задан"
        )
        return None

    if not GOOGLE_DRIVE_FOLDER_ID:
        logger.warning(
            "GOOGLE_DRIVE_FOLDER_ID не задан"
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

    except Exception:
        logger.exception(
            "Ошибка подключения к Google Drive"
        )
        return None


def find_drive_file(service):
    global drive_file_id

    query = (
        f"'{GOOGLE_DRIVE_FOLDER_ID}' "
        "in parents "
        f"and name = '{GOOGLE_DRIVE_FILENAME}' "
        "and trashed = false"
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

    if not files:
        return None

    drive_file_id = files[0]["id"]

    return drive_file_id


def save_logs_to_drive():
    global drive_file_id

    service = get_drive_service()

    if service is None:
        return False

    try:
        with drive_lock:

            data = {
                "updated_at": datetime.now(
                    timezone.utc
                ).isoformat(),

                "players": list(
                    known_players.values()
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

                (
                    service.files()
                    .update(
                        fileId=drive_file_id,
                        media_body=media,
                        fields="id",
                    )
                    .execute()
                )

                logger.info(
                    "Логи обновлены на Google Drive"
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
                    "Создан player_logs.json "
                    "на Google Drive"
                )

        return True

    except Exception:
        logger.exception(
            "Ошибка сохранения на Google Drive"
        )
        return False


def save_local_backup():
    try:
        data = {
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),

            "players": list(
                known_players.values()
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
            "Локальная резервная копия сохранена: %s",
            LOCAL_LOG_FILE,
        )

    except Exception:
        logger.exception(
            "Ошибка локального сохранения"
        )


def load_logs_from_drive():
    global drive_file_id
    global player_logs
    global known_players

    service = get_drive_service()

    if service is None:
        logger.warning(
            "Google Drive недоступен. "
            "Пробуем локальный JSON."
        )
        load_local_backup()
        return

    try:
        drive_file_id = find_drive_file(
            service
        )

        if not drive_file_id:

            logger.info(
                "player_logs.json на Google Drive "
                "ещё не существует."
            )

            load_local_backup()

            return

        request = (
            service.files()
            .get_media(
                fileId=drive_file_id
            )
        )

        buffer = io.BytesIO()

        downloader = MediaIoBaseDownload(
            buffer,
            request,
        )

        done = False

        while not done:
            _, done = (
                downloader.next_chunk()
            )

        raw = (
            buffer
            .getvalue()
            .decode("utf-8")
        )

        data = json.loads(raw)

        # Новый формат.
        if isinstance(data, dict):

            events = data.get(
                "events",
                []
            )

            players = data.get(
                "players",
                []
            )

            if isinstance(events, list):
                player_logs = events[
                    -MAX_PLAYER_LOGS:
                ]

            if isinstance(players, list):

                for player in players:

                    if not is_ignored_player(
                        player
                    ):

                        key = (
                            player
                            .casefold()
                        )

                        known_players[key] = (
                            player
                        )

        # Старый формат — просто список событий.
        elif isinstance(data, list):

            player_logs = data[
                -MAX_PLAYER_LOGS:
            ]

            for event in player_logs:

                player = event.get(
                    "player"
                )

                if (
                    player
                    and not is_ignored_player(
                        player
                    )
                ):

                    known_players[
                        player.casefold()
                    ] = player

        save_local_backup()

        logger.info(
            "Загружено с Google Drive: "
            "%s событий, %s игроков",
            len(player_logs),
            len(known_players),
        )

    except Exception:
        logger.exception(
            "Ошибка загрузки Google Drive"
        )

        load_local_backup()


def load_local_backup():
    global player_logs
    global known_players

    try:

        if not os.path.exists(
            LOCAL_LOG_FILE
        ):
            return

        with open(
            LOCAL_LOG_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(data, dict):

            events = data.get(
                "events",
                []
            )

            players = data.get(
                "players",
                []
            )

            if isinstance(events, list):
                player_logs = events[
                    -MAX_PLAYER_LOGS:
                ]

            if isinstance(players, list):

                for player in players:

                    if not is_ignored_player(
                        player
                    ):

                        known_players[
                            player.casefold()
                        ] = player

        elif isinstance(data, list):

            player_logs = data[
                -MAX_PLAYER_LOGS:
            ]

            for event in player_logs:

                player = event.get(
                    "player"
                )

                if (
                    player
                    and not is_ignored_player(
                        player
                    )
                ):

                    known_players[
                        player.casefold()
                    ] = player

        logger.info(
            "Загружена локальная резервная копия"
        )

    except Exception:
        logger.exception(
            "Ошибка загрузки локального JSON"
        )


def save_everything():
    save_local_backup()
    save_logs_to_drive()


# ============================================================
# ДОБАВЛЕНИЕ ИГРОКА
# ============================================================

def register_player(name):
    if is_ignored_player(name):
        return False

    name = str(name).strip()

    key = name.casefold()

    if key in known_players:
        return False

    known_players[key] = name

    logger.info(
        "Новый игрок добавлен в список: %s",
        name,
    )

    save_everything()

    return True


# ============================================================
# ДОБАВЛЕНИЕ СОБЫТИЯ
# ============================================================

def add_player_log(event, player):

    if is_ignored_player(player):
        return

    register_player(player)

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

    logger.info(
        "PLAYER %s: %s",
        event.upper(),
        player,
    )

    save_everything()


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
            "10000"
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

        if status.players.sample:

            for player in (
                status.players.sample
            ):

                if not player:
                    continue

                name = getattr(
                    player,
                    "name",
                    None,
                )

                if not name:
                    continue

                if is_ignored_player(
                    name
                ):
                    continue

                players.append(
                    name
                )

        # Убираем дубли.
        unique = {}

        for player in players:
            unique[
                player.casefold()
            ] = player

        players = list(
            unique.values()
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

        if (
            player.casefold()
            == target
        ):
            return player

    return None


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "🤖 <b>Minecraft Server Bot</b>\n\n"

        "Команды:\n"

        "/status — состояние сервера\n"

        "/online — количество игроков\n"

        "/players — все игроки онлайн\n"

        "/ap — все игроки из истории\n"

        "/target ник — отслеживать игрока\n"

        "/untarget — убрать отслеживание\n"

        "/log — включить/выключить уведомления\n"

        "/logs — последние события\n"

        "/logger — общий лог\n"

        "/logger ник — история игрока"
    )

    await update.message.reply_text(
        text,
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

        f"🌐 <code>"
        f"{MINECRAFT_IP}:"
        f"{MINECRAFT_PORT}"
        f"</code>\n"

        f"👥 Игроков: "
        f"<b>{data['players_online']}/"
        f"{data['players_max']}</b>\n"

        f"📡 Ping: "
        f"<b>{data['latency']} ms</b>"
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
        f"🟢 Игроков онлайн: "
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

    players = data["players"]

    text = (
        f"🟢 <b>Игроков онлайн: "
        f"{data['players_online']}/"
        f"{data['players_max']}</b>\n\n"
    )

    if not players:

        text += (
            "⚠️ Сервер не передал список "
            "игроков через status query."
        )

    else:

        text += (
            "👤 <b>Игроки:</b>\n"
        )

        for i, player in enumerate(
            players,
            1,
        ):

            text += (
                f"{i}. "
                f"<code>{player}</code>\n"
            )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
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
            "📋 Список игроков пока пуст."
        )

        return

    players = sorted(
        known_players.values(),
        key=str.casefold,
    )

    lines = [
        "👤 <b>Все игроки, "
        "которых видел бот:</b>\n"
    ]

    for i, player in enumerate(
        players,
        1,
    ):

        lines.append(
            f"{i}. <code>{player}</code>"
        )

    # Telegram имеет ограничение размера сообщения.
    text = "\n".join(lines)

    if len(text) <= 4000:

        await update.message.reply_text(
            text,
            parse_mode="HTML",
        )

        return

    # Если игроков очень много — разбиваем.
    chunk = ""

    for line in lines:

        if len(chunk) + len(line) > 3800:

            await update.message.reply_text(
                chunk,
                parse_mode="HTML",
            )

            chunk = ""

        chunk += line + "\n"

    if chunk:

        await update.message.reply_text(
            chunk,
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
            "/target ник"
        )

        return

    target_name = (
        " ".join(
            context.args
        ).strip()
    )

    data = get_server_status()

    last_seen = None

    if data["online"]:

        if data["players"]:

            found = find_player(
                data["players"],
                target_name,
            )

            last_seen = (
                found is not None
            )

    targets[chat_id] = {
        "name": target_name,
        "last_seen": last_seen,
    }

    if last_seen is True:

        status = (
            f"🟢 <b>{target_name}</b> "
            "сейчас онлайн."
        )

    elif last_seen is False:

        status = (
            f"🔴 <b>{target_name}</b> "
            "сейчас офлайн."
        )

    else:

        status = (
            "⚠️ Состояние пока "
            "неизвестно."
        )

    await update.message.reply_text(
        f"🎯 Отслеживание: "
        f"<b>{target_name}</b>\n\n"
        f"{status}\n\n"
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

    chat_id = update.effective_chat.id

    target = targets.pop(
        chat_id,
        None,
    )

    if not target:

        await update.message.reply_text(
            "Сейчас никто не отслеживается."
        )

        return

    await update.message.reply_text(
        f"🛑 Перестал отслеживать "
        f"<b>{target['name']}</b>.",
        parse_mode="HTML",
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
            "🛑 Уведомления о входах/выходах выключены."
        )

    else:

        log_subscribers.add(
            chat_id
        )

        await update.message.reply_text(
            "🟢 Уведомления о входах/выходах включены."
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
                    int(
                        context.args[0]
                    ),
                    50,
                ),
            )

        except ValueError:
            pass

    entries = player_logs[
        -limit:
    ]

    lines = [
        "📋 <b>Последние события</b>\n"
    ]

    for entry in entries:

        player = entry.get(
            "player",
            "",
        )

        if is_ignored_player(
            player
        ):
            continue

        event = entry.get(
            "event"
        )

        icon = (
            "🟢"
            if event == "join"
            else "🔴"
        )

        action = (
            "зашёл"
            if event == "join"
            else "вышел"
        )

        lines.append(
            f"{icon} "
            f"<code>{entry.get('time','')}</code> "
            f"— <b>{player}</b> {action}"
        )

    if len(lines) == 1:

        await update.message.reply_text(
            "📋 Подходящих записей нет."
        )

        return

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

    target = (
        " ".join(
            context.args
        ).casefold()
    )

    matches = []

    for entry in player_logs:

        player = entry.get(
            "player",
            "",
        )

        if is_ignored_player(
            player
        ):
            continue

        if (
            player.casefold()
            == target
        ):

            matches.append(
                entry
            )

    if not matches:

        await update.message.reply_text(
            "📋 Для этого игрока "
            "записей пока нет."
        )

        return

    real_name = matches[
        -1
    ]["player"]

    lines = [
        f"📋 <b>История "
        f"{real_name}</b>\n"
    ]

    for entry in matches[-50:]:

        event = entry.get(
            "event"
        )

        icon = (
            "🟢"
            if event == "join"
            else "🔴"
        )

        action = (
            "зашёл"
            if event == "join"
            else "вышел"
        )

        lines.append(
            f"{icon} "
            f"<code>{entry.get('time','')}</code> "
            f"— {action}"
        )

    await update.message.reply_text(
        "\n".join(lines),
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
        "Minecraft ONLINE — "
        "%s/%s игроков",
        data["players_online"],
        data["players_max"],
    )

    players = data["players"]

    # ========================================================
    # КАЖДУЮ ПРОВЕРКУ РЕГИСТРИРУЕМ ВЕСЬ СПИСОК
    # ========================================================

    for player in players:

        if is_ignored_player(
            player
        ):
            continue

        register_player(
            player
        )

    current_players = {
        player.casefold(): player
        for player in players
        if not is_ignored_player(
            player
        )
    }

    # ========================================================
    # ПЕРВЫЙ ОПРОС
    # ========================================================

    if previous_players is None:

        previous_players = (
            current_players
        )

        logger.info(
            "Первичный список игроков "
            "сохранён: %s",
            len(current_players),
        )

    else:

        # ====================================================
        # НОВЫЕ ИГРОКИ
        # ====================================================

        joined_keys = (
            set(current_players)
            - set(previous_players)
        )

        # ====================================================
        # ВЫШЕДШИЕ ИГРОКИ
        # ====================================================

        left_keys = (
            set(previous_players)
            - set(current_players)
        )

        # ====================================================
        # JOIN
        # ====================================================

        for key in joined_keys:

            player = (
                current_players[key]
            )

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
                            f"🟢 <b>{player}</b> "
                            "зашёл на сервер!"
                        ),
                        parse_mode="HTML",
                    )

                except Exception as e:

                    logger.warning(
                        "Ошибка уведомления: %s",
                        e,
                    )

        # ====================================================
        # LEAVE
        # ====================================================

        for key in left_keys:

            player = (
                previous_players[key]
            )

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
                            f"🔴 <b>{player}</b> "
                            "вышел с сервера!"
                        ),
                        parse_mode="HTML",
                    )

                except Exception as e:

                    logger.warning(
                        "Ошибка уведомления: %s",
                        e,
                    )

        previous_players = (
            current_players
        )

    # ========================================================
    # TARGET
    # ========================================================

    for chat_id, target in list(
        targets.items()
    ):

        target_name = target[
            "name"
        ]

        previous = target[
            "last_seen"
        ]

        current = (
            find_player(
                players,
                target_name,
            )
            is not None
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
                        f"🟢 <b>{target_name}</b> "
                        "зашёл на сервер!"
                    ),
                    parse_mode="HTML",
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔴 <b>{target_name}</b> "
                        "вышел с сервера!"
                    ),
                    parse_mode="HTML",
                )

        except Exception as e:

            logger.warning(
                "Ошибка target уведомления: %s",
                e,
            )


# ============================================================
# MAIN
# ============================================================

def main():

    # Flask для Render.
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

    # Загружаем историю.
    load_logs_from_drive()

    logger.info(
        "Запуск Telegram-бота..."
    )

    application = (
        Application
        .builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # ========================================================
    # COMMANDS
    # ========================================================

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
            "ap",
            ap_command,
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

    # ========================================================
    # AUTOMATIC CHECK
    # ========================================================

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
