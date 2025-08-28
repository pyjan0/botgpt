import logging
import httpx
import os
import base64
from io import BytesIO
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from config import TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-gpt-bot")

# DAN-инструкции
DAN_PROMPT = """
Ты полезный ассистент, который честно и понятно отвечает на вопросы.
"""

# Render URL (для webhook)
RENDER_URL = os.getenv("RENDER_URL")


# ===== GPT TEXT =====
async def chat_with_ai(user_id: int, message: str) -> str:
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


# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот 🤖\n"
        "Я умею работать с:\n"
        "📄 Текстом\n"
        "📷 Фото (с подписью и текстом)\n"
        "📂 Файлами (.py, .txt, .json)\n"
    )


# текстовые сообщения
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    reply = await chat_with_ai(update.effective_user.id, user_text)
    await update.message.reply_text(reply)


# фото + подпись + текст
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.photo[-1].get_file()
    file_bytes = await file.download_as_bytearray()

    # кодируем картинку в base64
    file_b64 = base64.b64encode(file_bytes).decode("utf-8")
    image_data = f"data:image/jpeg;base64,{file_b64}"

    # собираем caption и текст (если есть)
    caption = update.message.caption or ""
    extra_text = update.message.text or ""   # иногда Telegram кладёт текст в text
    full_question = (caption + " " + extra_text).strip()
    if not full_question:
        full_question = "Опиши это изображение"

    user_content = [
        {"type": "text", "text": f"Вопрос пользователя: {full_question}\nВот изображение:"},
        {"type": "image_url", "image_url": image_data}
    ]

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
                        {"role": "user", "content": user_content}
                    ],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        reply = "⚠️ Ошибка при обработке изображения"

    await update.message.reply_text(reply)


# файлы (.py, .txt, .json и т.д.)
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    file_bytes = await file.download_as_bytearray()
    filename = update.message.document.file_name

    try:
        # пробуем как текст
        text_content = file_bytes.decode("utf-8")
        prompt = f"Пользователь прислал файл {filename}. Объясни, что это за файл:\n\n{text_content[:4000]}"
        reply = await chat_with_ai(update.effective_user.id, prompt)
        await update.message.reply_text(reply)
    except UnicodeDecodeError:
        # если бинарь → просто вернуть
        await update.message.reply_document(BytesIO(file_bytes), filename=filename)


# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Webhook
    port = int(os.environ.get("PORT", 5000))
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
