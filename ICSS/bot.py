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
        "/players — список игроков",
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
        logger.info(
            "Minecraft OFFLINE"
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

    # Проверка Minecraft каждые 60 секунд
    application.job_queue.run_repeating(
        automatic_check,
        interval=60,
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