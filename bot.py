import os
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

WAITING_MINUTES = 10       # Время ожидания для всех по умолчанию (мин)
IRA_WAITING_MINUTES = 5    # Время ожидания для Иры (мин)
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


def fetch_conversations_paginated(status: str) -> List[dict]:
    """Глубокий сбор диалогов с явным проходом по страницам."""
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
    conversations = []
    page = 1

    while True:
        params = {"status": status, "assignee_type": "all", "page": page}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                logger.error(f"Chatwoot error ({status}, page {page}): {resp.status_code}")
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

            # Ограничитель предосторожности от бесконечного цикла
            if page > 50:
                break

        except Exception as e:
            logger.error(f"Error fetching page {page} for {status}: {e}")
            break

    return conversations


def get_agent_stats_from_api() -> Dict[str, int]:
    """
    Пытается получить точную статистику напрямую через Reports API Chatwoot.
    Если недоступно — использует полный обход списка диалогов.
    """
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/reports/agents"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            reports = resp.json()
            stats = {}
            for agent_data in reports:
                agent = agent_data.get("agent", {})
                name = agent.get("name") or agent.get("available_name")
                # Суммируем обработанные тикеты
                metric_count = agent_data.get("conversations_count", 0)
                if name and metric_count > 0:
                    stats[name] = metric_count
            if stats:
                return stats
    except Exception as e:
        logger.warning(f"Reports API unavailable, falling back to direct fetching: {e}")

    # Резервный метод: сбор диалогов вручную
    all_convs = []
    for st in ["open", "resolved", "pending"]:
        all_convs.extend(fetch_conversations_paginated(st))
    
    unique_convs = {c["id"]: c for c in all_convs}
    
    stats: Dict[str, int] = {}
    for conv in unique_convs.values():
        assignee = conv.get("meta", {}).get("assignee")
        if assignee:
            name = assignee.get("name", "Без имени").strip()
            stats[name] = stats.get(name, 0) + 1

    return stats


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
    conversations = fetch_conversations_paginated("open")
    now = datetime.now(timezone.utc)

    for conv in conversations:
        conv_id = conv.get("id")
        display_id = conv.get("display_id") or conv_id
        contact_name = conv.get("meta", {}).get("sender", {}).get("name", "Клиент")
        assignee = conv.get("meta", {}).get("assignee")
        assignee_name = assignee.get("name") if assignee else "Не назначен"

        is_ira = "ира" in assignee_name.lower() or "ira" in assignee_name.lower()
        required_wait_minutes = IRA_WAITING_MINUTES if is_ira else WAITING_MINUTES

        messages = get_conversation_messages(conv_id)
        if not messages:
            continue

        last_msg = messages[-1]
        msg_type = last_msg.get("message_type")

        if msg_type == 0:  # Сообщение от клиента
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
                    logger.info(f"Notification sent for conversation {conv_id}")
                except Exception as e:
                    logger.error(f"Telegram send error: {e}")

        elif msg_type == 1 and conv_id in already_notified:  # Ответ оператора
            already_notified.discard(conv_id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_agent_stats_from_api()

    if not stats:
        await update.message.reply_text("Назначенных тикетов не найдено.", parse_mode="HTML")
        return

    # Находим максимальное количество тикетов у одного оператора (принимаем за 100%)
    max_tickets = max(stats.values())
    total_assigned = sum(stats.values())

    lines = ["📊 <b>Статистика по операторам (активные + завершённые):</b>\n"]
    
    sorted_stats = sorted(stats.items(), key=lambda x: -x[1])
    
    for name, count in sorted_stats:
        # Расчет процента относительно лидера
        relative_pct = (count / max_tickets) * 100 if max_tickets > 0 else 0
        lines.append(f"• <b>{name}</b>: <b>{count}</b> ({relative_pct:.1f}%)")

    lines.append(f"\nВсего обработано тикетов: <b>{total_assigned}</b>")
    text = "\n".join(lines)

    await update.message.reply_text(text, parse_mode="HTML")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот мониторинга Chatwoot.\n\n"
        "Команды:\n"
        "/stats — общая статистика по назначенным тикетам"
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
