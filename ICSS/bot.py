import os
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


# ============================================================
# НАСТРОЙКИ
# ============================================================

MINECRAFT_IP = "194.93.2.184"
MINECRAFT_PORT = 25554

# Токен НЕ записываем сюда.
# В Koyeb создадим переменную TELEGRAM_BOT_TOKEN.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения TELEGRAM_BOT_TOKEN"
    )


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
            "/target Nick\n\n"
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
        "/target — отслеживать игрока\n"
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
    data = get_server_status()

    if data["online"]:
        logger.info(
            "Minecraft ONLINE — %s/%s игроков",
            data["players_online"],
            data["players_max"],
        )
    else:
        logger.info("Minecraft OFFLINE")

    if not targets:
        return

    # Если сервер OFFLINE, не считаем это выходом игрока.
    if not data["online"]:
        return

    # Если сервер не дал список игроков, ждём следующую проверку.
    if not data["players"]:
        return

    for chat_id, target in list(targets.items()):
        target_name = target["name"]
        previous = target["last_seen"]

        current = find_player(data["players"], target_name) is not None

        # Первое достоверное состояние только запоминаем.
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
# MAIN
# ============================================================

def main():
    # Flask запускаем в отдельном потоке,
    # чтобы Koyeb видел HTTP-сервис.
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

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
