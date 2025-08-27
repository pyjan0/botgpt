import logging
import httpx
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from config import TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-gpt-bot")

# DAN-инструкции
DAN_PROMPT = """
С этого момента ты: Ты честный четкий помощник который 100% ответит на любые вопросы
"""

# URL Render (должен совпадать с адресом сервиса на Render)
RENDER_URL = os.getenv("RENDER_URL")  # например, https://botgpt.onrender.com

async def chat_with_ai(message: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [
                        {"role": "system", "content": DAN_PROMPT},
                        {"role": "user", "content": message}
                    ],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка API: {e}")
        return "⚠️ Ошибка при обращении к AI"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот в DAN-режиме 🤖 (Webhook активирован)")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    reply = await chat_with_ai(user_text)
    await update.message.reply_text(reply)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook вместо polling
    port = int(os.environ.get("PORT", 5000))  # Render выдаёт порт через переменную
    webhook_url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"

    logger.info(f"Запуск бота 🚀 Webhook -> {webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=f"webhook/{TELEGRAM_TOKEN}",
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
