import os
import io
import json
import threading
import logging

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

# Токен НЕ записываем сюда.
# В Render создадим переменную TELEGRAM_BOT_TOKEN.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения TELEGRAM_BOT_TOKEN"
    )

# Google Drive
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_DRIVE_FILENAME = os.environ.get("GOOGLE_DRIVE_FILENAME", "player_logs.json")
LOCAL_LOG_FILE = "player_logs.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]



# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Отслеживаемый игрок для каждого Telegram-чата.
# Формат: {chat_id: {"name": "...", "last_seen": True/False/None}}
targets = {}

# Как часто проверять отслеживаемого игрока.
TARGET_CHECK_INTERVAL = 10

# Лог входов/выходов игроков. Храним последние 200 событий.
player_logs = []
MAX_PLAYER_LOGS = 200

# ID файла player_logs.json на Google Drive.
drive_file_id = None

def get_drive_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_DRIVE_FOLDER_ID:
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=DRIVE_SCOPES
        )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except Exception as e:
        logger.exception("Ошибка подключения к Google Drive: %s", e)
        return None

def save_logs_local():
    try:
        with open(LOCAL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(player_logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Не удалось сохранить локальный JSON: %s", e)

def sync_logs_to_drive():
    global drive_file_id
    service = get_drive_service()
    if service is None:
        return
    try:
        data = json.dumps(player_logs, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/json", resumable=False)

        if drive_file_id is None:
            result = service.files().list(
                q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and name = '{GOOGLE_DRIVE_FILENAME}' and trashed = false",
                spaces="drive", fields="files(id,name)", pageSize=10
            ).execute()
            files = result.get("files", [])
            if files:
                drive_file_id = files[0]["id"]

        if drive_file_id:
            service.files().update(
                fileId=drive_file_id, media_body=media, fields="id"
            ).execute()
        else:
            metadata = {"name": GOOGLE_DRIVE_FILENAME, "parents": [GOOGLE_DRIVE_FOLDER_ID]}
            created = service.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
            drive_file_id = created["id"]

        logger.info("player_logs.json загружен на Google Drive")
    except Exception as e:
        logger.exception("Ошибка синхронизации с Google Drive: %s", e)

def load_logs_from_drive():
    global drive_file_id, player_logs
    service = get_drive_service()
    if service is None:
        return
    try:
        result = service.files().list(
            q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and name = '{GOOGLE_DRIVE_FILENAME}' and trashed = false",
            spaces="drive", fields="files(id,name)", pageSize=10
        ).execute()
        files = result.get("files", [])
        if not files:
            logger.info("player_logs.json на Google Drive ещё нет")
            return

        drive_file_id = files[0]["id"]
        request = service.files().get_media(fileId=drive_file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        loaded = json.loads(buffer.getvalue().decode("utf-8"))
        if isinstance(loaded, list):
            player_logs = loaded[-MAX_PLAYER_LOGS:]
            save_logs_local()
            logger.info("Загружено %s событий из Google Drive", len(player_logs))
    except Exception as e:
        logger.exception("Ошибка загрузки логов с Google Drive: %s", e)


# Последний достоверно известный список игроков.
previous_players = None

# Чаты, в которые отправляются автоматические уведомления о входе/выходе.
log_subscribers = set()


# ============================================================
# FLASK ДЛЯ KOYEB
# ============================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Minecraft Telegram Bot is running", 200


@web_app.route("/health")
def health():
    return "OK", 200


def run_web():
    port = int(os.environ.get("PORT", "8000"))

    web_app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# ПРОВЕРКА MINECRAFT
# ============================================================

def get_server_status():
    try:
        address = f"{MINECRAFT_IP}:{MINECRAFT_PORT}"

        server = JavaServer.lookup(address)

        status = server.status()

        players_online = status.players.online
        players_max = status.players.max

        players = []

        if status.players.sample:
            for player in status.players.sample:
                if player.name:
                    players.append(player.name)

        return {
            "online": True,
            "players_online": players_online,
            "players_max": players_max,
            "players": players,
            "latency": round(status.latency),
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
# ФОРМИРОВАНИЕ СТАТУСА
# ============================================================

def make_status_text():
    data = get_server_status()

    if not data["online"]:
        return (
            "🔴 <b>Сервер OFFLINE</b>\n\n"
            f"🌐 {MINECRAFT_IP}:{MINECRAFT_PORT}"
        )

    text = (
        "🟢 <b>Сервер ONLINE</b>\n\n"
        f"🌐 <code>{MINECRAFT_IP}:{MINECRAFT_PORT}</code>\n"
        f"👥 Игроков: <b>{data['players_online']}/{data['players_max']}</b>\n"
        f"📡 Ping: <b>{data['latency']} ms</b>"
    )

    if data["players"]:
        text += "\n\n👤 <b>Игроки:</b>\n"

        for player in data["players"]:
            text += f"• {player}\n"

    elif data["players_online"] > 0:
        text += "\n\n👤 Список игроков сервер не предоставил."

    return text


# ============================================================
# /TARGET
# ============================================================

def find_player(players, target_name):
    """Ищет игрока без учёта регистра."""
    target = target_name.casefold()

    for player in players:
        if player.casefold() == target:
            return player

    return None


async def target_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/target <ник>\n\n"
            "После этого бот будет сообщать о входе и выходе игрока."
        )
        return

    target_name = " ".join(context.args).strip()

    data = get_server_status()

    if not data["online"]:
        last_seen = None
        status_text = (
            "🔴 Сервер сейчас OFFLINE.\n"
            "Начальное состояние игрока определю при следующей проверке."
        )
    elif not data["players"]:
        last_seen = None
        status_text = (
            "⚠️ Сервер сейчас не предоставляет список игроков.\n"
            "Состояние игрока определю, когда список снова станет доступен."
        )
    else:
        found = find_player(data["players"], target_name)
        last_seen = found is not None

        if found:
            status_text = f"🟢 <b>{found}</b> сейчас онлайн."
        else:
            status_text = f"🔴 <b>{target_name}</b> сейчас не на сервере."

    targets[chat_id] = {
        "name": target_name,
        "last_seen": last_seen,
    }

    await update.message.reply_text(
        f"🎯 Теперь отслеживаю <b>{target_name}</b>.\n\n"
        f"{status_text}\n\n"
        f"🔄 Проверка каждые {TARGET_CHECK_INTERVAL} секунд.",
        parse_mode="HTML",
    )


async def untarget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    if chat_id not in targets:
        await update.message.reply_text("Сейчас никто не отслеживается.")
        return

    name = targets[chat_id]["name"]
    del targets[chat_id]

    await update.message.reply_text(
        f"🛑 Перестал отслеживать <b>{name}</b>.",
        parse_mode="HTML",
    )


# ============================================================
# ЛОГИ ИГРОКОВ
# ============================================================

def add_player_log(event, player):
    """Добавляет событие в историю и ограничивает её размер."""
    from datetime import datetime, timezone

    entry = {
        "time": datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M:%S"),
        "event": event,
        "player": player,
    }

    player_logs.append(entry)

    if len(player_logs) > MAX_PLAYER_LOGS:
        del player_logs[:-MAX_PLAYER_LOGS]

    save_logs_local()
    sync_logs_to_drive()

    logger.info(
        "PLAYER %s: %s",
        event.upper(),
        player,
    )


async def log_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = update.effective_chat.id

    if chat_id in log_subscribers:
        log_subscribers.remove(chat_id)
        await update.message.reply_text(
            "🛑 Общий лог игроков выключен для этого чата."
        )
        return

    log_subscribers.add(chat_id)

    await update.message.reply_text(
        "📋 Общий лог игроков включён.\n\n"
        "Теперь бот будет сообщать сюда, когда игроки заходят на сервер или выходят с него."
    )


async def logs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not player_logs:
        await update.message.reply_text(
            "📋 Лог пока пуст.\n"
            "События начнут появляться после следующего изменения списка игроков."
        )
        return

    # По умолчанию показываем последние 20, можно /logs 50.
    limit = 20

    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    entries = player_logs[-limit:]

    lines = ["📋 <b>Лог игроков</b>\n"]

    for entry in entries:
        if entry["event"] == "join":
            icon = "🟢"
            action = "зашёл"
        else:
            icon = "🔴"
            action = "вышел"

        lines.append(
            f"{icon} <code>{entry['time']}</code> — "
            f"<b>{entry['player']}</b> {action}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
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
        "Команды:\n"
        "/status — состояние сервера\n"
        "/online — количество игроков\n"
        "/players — список игроков\n"
        "/target <ник> — отслеживать игрока\n/log — включить/выключить общий лог\n/logs — последние события\n"
        "/untarget — перестать отслеживать",
        parse_mode="HTML",
    )


# ============================================================
# /STATUS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = make_status_text()

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
        f"👥 Игроков: {data['players_online']}/{data['players_max']}"
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
            f"👥 Игроков: {data['players_online']}/{data['players_max']}\n\n"
            "Список игроков сервер не предоставил."
        )
        return

    text = (
        f"🟢 Игроков онлайн: "
        f"{data['players_online']}/{data['players_max']}\n\n"
        "👤 <b>Игроки:</b>\n"
    )

    for player in data["players"]:
        text += f"• {player}\n"

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# АВТООБНОВЛЕНИЕ
# ============================================================

async def automatic_check(
    context: ContextTypes.DEFAULT_TYPE,
):
    global previous_players

    data = get_server_status()

    if data["online"]:
        logger.info(
            "Minecraft ONLINE — %s/%s игроков",
            data["players_online"],
            data["players_max"],
        )
    else:
        logger.info("Minecraft OFFLINE")
        # Не считаем отключение сервера выходом всех игроков.
        return

    # Без sample списка нельзя надёжно определить, кто именно вошёл/вышел.
    if not data["players"]:
        return

    current_players = {
        player.casefold(): player
        for player in data["players"]
    }

    # Первый успешный опрос только запоминаем.
    if previous_players is None:
        previous_players = current_players
    else:
        joined_keys = set(current_players) - set(previous_players)
        left_keys = set(previous_players) - set(current_players)

        for key in joined_keys:
            player = current_players[key]
            add_player_log("join", player)

            for chat_id in list(log_subscribers):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🟢 <b>{player}</b> зашёл на сервер!",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить лог в чат %s: %s",
                        chat_id,
                        e,
                    )

        for key in left_keys:
            player = previous_players[key]
            add_player_log("leave", player)

            for chat_id in list(log_subscribers):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔴 <b>{player}</b> вышел с сервера!",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить лог в чат %s: %s",
                        chat_id,
                        e,
                    )

        previous_players = current_players

    # Отдельный /target продолжает работать независимо от общего лога.
    if not targets:
        return

    for chat_id, target in list(targets.items()):
        target_name = target["name"]
        previous = target["last_seen"]
        current = find_player(data["players"], target_name) is not None

        if previous is None:
            target["last_seen"] = current
            continue

        if current == previous:
            continue

        target["last_seen"] = current

        try:
            if current:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🟢 <b>{target_name}</b> зашёл на сервер!",
                    parse_mode="HTML",
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔴 <b>{target_name}</b> вышел с сервера!",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning(
                "Не удалось отправить уведомление в чат %s: %s",
                chat_id,
                e,
            )


# ============================================================
# /LOGGER — алиас для логов
# ============================================================

async def logger_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        target = " ".join(context.args).casefold()
        matches = [e for e in player_logs if e["player"].casefold() == target]
        if not matches:
            await update.message.reply_text("📋 Для этого игрока записей пока нет.")
            return
        lines = [f"📋 <b>История {matches[-1]['player']}</b>\n"]
        for e in matches[-50:]:
            icon = "🟢" if e["event"] == "join" else "🔴"
            action = "зашёл" if e["event"] == "join" else "вышел"
            lines.append(f"{icon} <code>{e['time']}</code> — {action}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    await logs_command(update, context)


async def ap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает игроков, которые присутствуют в истории логов."""
    if not player_logs:
        await update.message.reply_text("📋 История игроков пока пуста.")
        return
    names = {}
    for e in player_logs:
        names[e["player"].casefold()] = e["player"]
    lines = ["👤 <b>Игроки из истории:</b>"]
    for name in sorted(names.values(), key=str.casefold):
        lines.append(f"• {name}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# MAIN
# ============================================================

def main():
    # Flask запускаем в отдельном потоке,
    # чтобы Render видел HTTP-сервис.
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

    load_logs_from_drive()

    logger.info(
        "Запуск Telegram-бота..."
    )

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Команды
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CommandHandler("online", online_command)
    )

    application.add_handler(
        CommandHandler("players", players_command)
    )

    application.add_handler(
        CommandHandler("target", target_command)
    )

    application.add_handler(
        CommandHandler("untarget", untarget_command)
    )

    application.add_handler(
        CommandHandler("log", log_command)
    )

    application.add_handler(
        CommandHandler("logs", logs_command)
    )

    application.add_handler(
        CommandHandler("logger", logger_command)
    )

    application.add_handler(
        CommandHandler("ap", ap_command)
    )

    # Проверка Minecraft каждые 10 секунд
    application.job_queue.run_repeating(
        automatic_check,
        interval=TARGET_CHECK_INTERVAL,
        first=10,
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
