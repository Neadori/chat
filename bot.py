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

# Создаём Application один раз
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()


def get_open_conversations():
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
    params = {"status": "open", "assignee_type": "all", "page": 1}
    all_conversations = []

    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                logger.error(f"Chatwoot error: {resp.status_code} - {resp.text}")
                break

            data = resp.json()
            conversations = data.get("data", {}).get("payload", [])
            all_conversations.extend(conversations)

            meta = data.get("data", {}).get("meta", {})
            if not meta.get("next_page"):
                break
            params["page"] += 1
        except Exception as e:
            logger.error(f"Error fetching conversations: {e}")
            break

    return all_conversations


def get_conversation_messages(conversation_id: int):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("payload", [])
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
    return []


async def check_waiting_tickets():
    logger.info("Checking waiting tickets...")
    conversations = get_open_conversations()
    now = datetime.now(timezone.utc)

    for conv in conversations:
        conv_id = conv.get("id")
        display_id = conv.get("display_id") or conv_id
        contact_name = conv.get("meta", {}).get("sender", {}).get("name", "Клиент")
        assignee = conv.get("meta", {}).get("assignee")
        assignee_name = assignee.get("name") if assignee else "Не назначен"

        messages = get_conversation_messages(conv_id)
        if not messages:
            continue

        last_msg = messages[-1]
        msg_type = last_msg.get("message_type")

        if msg_type == 0:
            created_at = last_msg.get("created_at")
            if not created_at:
                continue

            msg_time = datetime.fromtimestamp(created_at, tz=timezone.utc)
            minutes_waiting = (now - msg_time).total_seconds() / 60

            if minutes_waiting >= WAITING_MINUTES and conv_id not in already_notified:
                text = (
                    f"⚠️ <b>Тикет ждёт ответа уже {int(minutes_waiting)} мин.</b>\n\n"
                    f"Клиент: <b>{contact_name}</b>\n"
                    f"Диалог: #{display_id}\n"
                    f"Назначен: <b>{assignee_name}</b>\n"
                    f"ID: <code>{conv_id}</code>"
                )
                try:
                    await telegram_app.bot.send_message(
                        chat_id=NOTIFY_CHAT_ID, text=text, parse_mode="HTML"
                    )
                    already_notified.add(conv_id)
                    logger.info(f"Notification sent for conversation {conv_id}")
                except Exception as e:
                    logger.error(f"Telegram send error: {e}")

        elif msg_type == 1 and conv_id in already_notified:
            already_notified.discard(conv_id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations = get_open_conversations()
    stats: Dict[str, int] = {}
    unassigned = 0

    for conv in conversations:
        assignee = conv.get("meta", {}).get("assignee")
        if assignee:
            name = assignee.get("name", "Без имени")
            stats[name] = stats.get(name, 0) + 1
        else:
            unassigned += 1

    if not stats and unassigned == 0:
        text = "Нет открытых тикетов 🎉"
    else:
        lines = ["📊 <b>Открытые тикеты:</b>\n"]
        for name, count in sorted(stats.items(), key=lambda x: -x[1]):
            lines.append(f"• {name}: <b>{count}</b>")
        if unassigned:
            lines.append(f"• Не назначены: <b>{unassigned}</b>")
        lines.append(f"\nВсего: <b>{sum(stats.values()) + unassigned}</b>")
        text = "\n".join(lines)

    await update.message.reply_text(text, parse_mode="HTML")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот мониторинга Chatwoot.\n\n"
        "Команды:\n"
        "/stats — сколько тикетов у каждого оператора"
    )


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("stats", stats_command))


@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)


@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    data = await request.json()
    event = data.get("event")
    logger.info(f"Chatwoot webhook: {event}")
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "Bot is running", "bot": "@Neadoribot"}


@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = f"{WEBHOOK_URL}/telegram"
    await telegram_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Telegram webhook set to: {webhook_url}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_waiting_tickets, "interval", seconds=CHECK_INTERVAL)
    scheduler.start()
    logger.info("Scheduler started")


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
