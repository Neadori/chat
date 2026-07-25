import os
import logging
from datetime import datetime, timezone
from typing import Dict, Set

import requests
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

# ================== НАСТРОЙКИ ==================
CHATWOOT_URL = os.getenv("CHATWOOT_URL", "https://support-proxy.liberty-tech.ru")
CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "1")
CHATWOOT_TOKEN = os.getenv("CHATWOOT_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "1498669791")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://chat-2qjr.onrender.com")

WAITING_MINUTES = 10
CHECK_INTERVAL = 60
# =================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
already_notified: Set[int] = set()

headers = {
    "api_access_token": CHATWOOT_TOKEN,
    "api-access-token": CHATWOOT_TOKEN,
    "Api-Access-Token": CHATWOOT_TOKEN,
    "Content-Type": "application/json"
}

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()


def get_open_conversations():
    # Пробуем несколько вариантов URL
    urls_to_try = [
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations?status=open",
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations",
        f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations?status=open&assignee_type=all",
    ]

    for url in urls_to_try:
        logger.info(f"Trying URL: {url}")
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            logger.info(f"Status: {resp.status_code} | Response: {resp.text[:300]}")
            
            if resp.status_code == 200:
                data = resp.json()
                conversations = data.get("data", {}).get("payload", [])
                logger.info(f"Found {len(conversations)} conversations")
                return conversations
        except Exception as e:
            logger.error(f"Error: {e}")

    return []


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations = get_open_conversations()
    
    if not conversations:
        await update.message.reply_text("Не удалось получить тикеты из Chatwoot")
        return

    stats: Dict[str, int] = {}

    for conv in conversations:
        assignee = conv.get("meta", {}).get("assignee")
        if assignee:
            name = assignee.get("name", "Без имени")
            stats[name] = stats.get(name, 0) + 1

    total = sum(stats.values())

    if total == 0:
        await update.message.reply_text("Нет открытых назначенных тикетов 🎉")
        return

    lines = [f"📊 <b>Открытые тикеты ({total}):</b>\n"]
    
    for name, count in sorted(stats.items(), key=lambda x: -x[1]):
        percent = round(count / total * 100)
        lines.append(f"• {name}: <b>{count}</b> ({percent}%)")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот мониторинга Chatwoot.\n\n"
        "Команды:\n"
        "/stats — статистика по тикетам"
    )


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("stats", stats_command))


@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)


@app.get("/")
async def root():
    return {"status": "Bot is running"}


@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram")
    logger.info("Bot started")


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
