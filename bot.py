import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Set, List

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

WAITING_MINUTES = 10       # Время ожидания для всех (мин)
IRA_WAITING_MINUTES = 5    # Время ожидания для Иры (мин)
CHECK_INTERVAL = 60
STATS_FILE = "daily_stats.json"
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

# ================== РАБОТА С БАЗОЙ ДАННЫХ ДНЯ ==================
def load_daily_data() -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return data
        except Exception as e:
            logger.error(f"Error loading stats file: {e}")
    return {"date": today_str, "assigned": {}}

def save_daily_data(data: dict):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving stats file: {e}")

def record_conversation_assignee(conv_id: int, assignee_name: str):
    if not assignee_name or assignee_name in ["Не назначен", "Без имени"]:
        return
    data = load_daily_data()
    conv_str = str(conv_id)
    # Если за день оператора у этого тикета еще не фиксировали
    if data["assigned"].get(conv_str) != assignee_name:
        data["assigned"][conv_str] = assignee_name
        save_daily_data(data)
        logger.info(f"Recorded ticket #{conv_id} for operator '{assignee_name}'")

# ================== CHATWOOT API ==================
def fetch_conversations_paginated(status: str) -> List[dict]:
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
    conversations = []
    page = 1

    while True:
        params = {"status": status, "assignee_type": "all", "page": page}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                break

            data = resp.json()
            payload = []
            
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], dict):
                    payload = data["data"].get("payload", [])
                elif "payload" in data:
                    payload = data.get("payload", [])
            elif isinstance(data, list):
                payload = data

            if not payload:
                break

            conversations.extend(payload)
            page += 1
            if page > 50:
                break
        except Exception as e:
            logger.error(f"Error fetching page {page}: {e}")
            break

    return conversations

def get_conversation_messages(conversation_id: int):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("payload", [])
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
    return []

def sync_active_conversations():
    """Синхронизация активных и закрытых тикетов из API в локальную базу."""
    for status in ["open", "resolved", "pending"]:
        convs = fetch_conversations_paginated(status)
        for conv in convs:
            conv_id = conv.get("id")
            assignee = conv.get("meta", {}).get("assignee")
            if conv_id and assignee:
                name = assignee.get("name")
                if name:
                    record_conversation_assignee(conv_id, name.strip())

# ================== ПРОВЕРКА ТАЙМАУТОВ ==================
async def check_waiting_tickets():
    logger.info("Checking waiting tickets...")
    conversations = fetch_conversations_paginated("open")
    now = datetime.now(timezone.utc)

    for conv in conversations:
        conv_id = conv.get("id")
        display_id = conv.get("display_id") or conv_id
        contact_name = conv.get("meta", {}).get("sender", {}).get("name", "Клиент")
        assignee = conv.get("meta", {}).get("assignee")
        assignee_name = assignee.get("name") if assignee else "Не назначен"

        if assignee and assignee.get("name"):
            record_conversation_assignee(conv_id, assignee.get("name").strip())

        is_ira = "ира" in assignee_name.lower() or "ira" in assignee_name.lower()
        required_wait_minutes = IRA_WAITING_MINUTES if is_ira else WAITING_MINUTES

        messages = get_conversation_messages(conv_id)
        if not messages:
            continue

        last_msg = messages[-1]
        msg_type = last_msg.get("message_type")

        if msg_type == 0:  # Клиент
            created_at = last_msg.get("created_at")
            if not created_at:
                continue

            msg_time = datetime.fromtimestamp(created_at, tz=timezone.utc)
            minutes_waiting = (now - msg_time).total_seconds() / 60

            if minutes_waiting >= required_wait_minutes and conv_id not in already_notified:
                text = (
                    f"⚠️ <b>Тикет ждёт ответа уже {int(minutes_waiting)} мин.</b>\n\n"
                    f"Клиент: <b>{contact_name}</b>\n"
                    f"Диалог: #{display_id}\n"
                    f"Назначен: <b>{assignee_name}</b>\n"
                    f"ID: <code>{conv_id}</code>"
                )
                try:
                    await telegram_app.bot.send_message(chat_id=NOTIFY_CHAT_ID, text=text, parse_mode="HTML")
                    already_notified.add(conv_id)
                except Exception as e:
                    logger.error(f"Telegram send error: {e}")

        elif msg_type == 1 and conv_id in already_notified:
            already_notified.discard(conv_id)

# ================== КОМАНДЫ ТЕЛЕГРАМ ==================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # При вызове /stats обновляем текущие тикеты из API
    sync_active_conversations()
    
    data = load_daily_data()
    assigned_dict = data.get("assigned", {})

    if not assigned_dict:
        await update.message.reply_text("За сегодня обработанных тикетов не зафиксировано.", parse_mode="HTML")
        return

    # Подсчитываем сколько тикетов запомнено за день у каждого оператора
    stats: Dict[str, int] = {}
    for conv_id, name in assigned_dict.items():
        stats[name] = stats.get(name, 0) + 1

    max_tickets = max(stats.values()) if stats else 1
    total_assigned = sum(stats.values())

    lines = ["📊 <b>Запомненная статистика за день (включая снятые/закрытые):</b>\n"]
    sorted_stats = sorted(stats.items(), key=lambda x: -x[1])

    for name, count in sorted_stats:
        relative_pct = (count / max_tickets) * 100
        lines.append(f"• <b>{name}</b>: <b>{count}</b> ({relative_pct:.1f}%)")

    lines.append(f"\nВсего зарегистрировано за день: <b>{total_assigned}</b>")
    text = "\n".join(lines)

    await update.message.reply_text(text, parse_mode="HTML")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот мониторинга Chatwoot.\n\n"
        "Команды:\n"
        "/stats — статистика за день (с учётом снятых и закрытых)"
    )

telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("stats", stats_command))

# ================== WEBHOOKS ==================
@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    """Принимает изменения статусов и назначений от Chatwoot в реальном времени."""
    try:
        data = await request.json()
        event = data.get("event")
        
        # Перехватываем создание сообщений или обновление диалога
        if event in ["conversation_created", "conversation_updated", "message_created"]:
            conv_id = data.get("id") or data.get("conversation", {}).get("id")
            meta = data.get("meta", {}) or data.get("conversation", {}).get("meta", {})
            assignee = meta.get("assignee")
            
            if conv_id and assignee:
                name = assignee.get("name")
                if name:
                    record_conversation_assignee(conv_id, name.strip())
    except Exception as e:
        logger.error(f"Error handling Chatwoot webhook: {e}")

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

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_waiting_tickets, "interval", seconds=CHECK_INTERVAL)
    scheduler.start()

@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
