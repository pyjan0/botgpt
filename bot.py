import logging
import os
import base64
from io import BytesIO
import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ================== НАСТРОЙКИ ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-webhook-bot")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8033358653"))
DEFAULT_TOKENS = 20
TEXT_COST = 1
PHOTO_COST = 2
DOC_COST = 2

FIREBASE_URL = "https://botgpttok-default-rtdb.europe-west1.firebasedatabase.app"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL")
RENDER_URL = os.getenv("RENDER_URL")  # Например: https://myapp.onrender.com

DAN_PROMPT = """
Ты полезный ассистент, который честно и понятно отвечает на вопросы.
Если вместе с фото есть текстовый вопрос — приоритет отдавай вопросу, а фото используй как контекст.
Если прислан файл с текстом — объясни, что это за файл и что он делает. Будь кратким и по делу.
"""

TEXT_LIKE = {".txt", ".py", ".json", ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv"}

# ================== FIREBASE ==================
async def firebase_get(path: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{FIREBASE_URL}/{path}.json")
        r.raise_for_status()
        return r.json() or {}

async def firebase_put(path: str, data):
    async with httpx.AsyncClient() as client:
        r = await client.put(f"{FIREBASE_URL}/{path}.json", json=data)
        r.raise_for_status()
        return r.json()

async def firebase_patch(path: str, data):
    async with httpx.AsyncClient() as client:
        r = await client.patch(f"{FIREBASE_URL}/{path}.json", json=data)
        r.raise_for_status()
        return r.json()

# ================== ХЕЛПЕРЫ ==================
def is_admin(user_id: int):
    return user_id == ADMIN_ID

async def ensure_user(user_id: int):
    user = await firebase_get(f"users/{user_id}")
    if not user:
        await firebase_put(f"users/{user_id}", {"tokens": DEFAULT_TOKENS})

async def get_tokens(user_id: int):
    user = await firebase_get(f"users/{user_id}")
    return user.get("tokens", DEFAULT_TOKENS) if user else DEFAULT_TOKENS

async def add_tokens(user_id: int, amount: int):
    await ensure_user(user_id)
    tokens = await get_tokens(user_id)
    await firebase_patch(f"users/{user_id}", {"tokens": tokens + amount})

async def use_tokens(user_id: int, amount: int):
    await ensure_user(user_id)
    tokens = await get_tokens(user_id)
    if tokens >= amount:
        await firebase_patch(f"users/{user_id}", {"tokens": tokens - amount})
        return True
    return False

async def refund_tokens(user_id: int, amount: int):
    await add_tokens(user_id, amount)

async def create_promo(code: str, amount: int):
    await firebase_put(f"promos/{code}", {"amount": amount})

async def redeem_promo(user_id: int, code: str):
    promo = await firebase_get(f"promos/{code}")
    if promo:
        await add_tokens(user_id, int(promo["amount"]))
        await firebase_patch(f"promos", {code: None})
        return True
    return False

# ================== МЕНЮ ==================
user_menu = ReplyKeyboardMarkup(
    [["💰 Мой баланс", "➕ Пополнить (промокод)"], ["ℹ️ Помощь"]],
    resize_keyboard=True
)
admin_menu = ReplyKeyboardMarkup(
    [["💎 Выдать токены"], ["🔙 Назад"]],
    resize_keyboard=True
)

# ================== GPT ==================
async def openrouter_chat(messages: list, model: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={"model": model, "messages": messages},
                timeout=60
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка AI: {e}")
        return "⚠️ Ошибка при обработке AI."

async def chat_with_ai_text(user_id: int, message: str) -> str:
    msgs = [{"role": "system", "content": DAN_PROMPT}, {"role": "user", "content": message}]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_file(filename: str, text: str) -> str:
    snippet = text[:8000]
    prompt = f"Пользователь прислал файл {filename}. Объясни что это, что делает, возможные проблемы.\n\n{snippet}"
    msgs = [{"role": "system", "content": DAN_PROMPT}, {"role": "user", "content": prompt}]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_image(user_question: str, b64_image: str) -> str:
    user_content = [
        {"type": "text", "text": f"Вопрос: {user_question}"},
        {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64_image}"},
    ]
    msgs = [{"role": "system", "content": DAN_PROMPT}, {"role": "user", "content": user_content}]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

# ================== ОБРАБОТЧИКИ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid)
    kb = admin_menu if is_admin(uid) else user_menu
    await update.message.reply_text("Привет! Я GPT-бот 🤖", reply_markup=kb)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tokens = await get_tokens(uid)
    await update.message.reply_text(f"💰 Ваш баланс: {tokens} токенов.")

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Введите промокод так: /redeem КОД")
        return
    code = context.args[0].strip()
    if await redeem_promo(uid, code):
        await update.message.reply_text("✅ Промокод применён! Баланс пополнен.")
    else:
        await update.message.reply_text("❌ Неверный или уже использованный промокод.")

async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    uid = update.effective_user.id
    if txt == "💰 Мой баланс":
        await balance(update, context)
    elif txt == "➕ Пополнить (промокод)":
        await update.message.reply_text("Пополнить можно у @G0ODMEN")
    elif txt == "ℹ️ Помощь":
        await update.message.reply_text(
            "ℹ️ Команды:\n"
            "/start — начать\n"
            "/balance — баланс\n"
            "➕ Пополнить (только текст)\n"
            "💎 Выдать токены (только для админа)"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid)
    if not await use_tokens(uid, TEXT_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    reply = await chat_with_ai_text(uid, update.message.text)
    await update.message.reply_text(reply)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid)
    if not await use_tokens(uid, PHOTO_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    file = await update.message.photo[-1].get_file()
    file_bytes = await file.download_as_bytearray()
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    question = (update.message.caption or "Что изображено на фото?").strip()
    reply = await chat_with_ai_image(question, b64)
    await update.message.reply_text(reply)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid)
    if not await use_tokens(uid, DOC_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    doc = update.message.document
    filename = doc.file_name or "file"
    ext = os.path.splitext(filename.lower())[1]
    tgfile = await doc.get_file()
    file_bytes = await tgfile.download_as_bytearray()
    if ext in TEXT_LIKE:
        try:
            text = file_bytes.decode("utf-8", errors="replace")
        except:
            text = "<не удалось прочитать файл>"
        reply = await chat_with_ai_file(filename, text)
        await update.message.reply_text(reply)
    else:
        await update.message.reply_document(BytesIO(file_bytes), filename=filename)
        await update.message.reply_text("Это бинарный файл — вернул его обратно.")

# ================== АДМИН ==================
(ADMIN_MENU, ASK_USER_ID, ASK_AMOUNT) = range(3)

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Доступ запрещен")
        return ConversationHandler.END
    await update.message.reply_text("🔧 Админ-меню", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "💎 Выдать токены":
        await update.message.reply_text("Введите USER_ID:", reply_markup=ReplyKeyboardRemove())
        return ASK_USER_ID
    elif txt == "🔙 Назад":
        await update.message.reply_text("Вы вышли из админ-меню.", reply_markup=user_menu)
        return ConversationHandler.END
    return ADMIN_MENU

async def admin_ask_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("Нужен числовой USER_ID.")
        return ASK_USER_ID
    context.user_data["target_id"] = int(txt)
    await update.message.reply_text("Сколько токенов выдать?")
    return ASK_AMOUNT

async def admin_ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("Введите целое число.")
        return ASK_AMOUNT
    target_id = context.user_data["target_id"]
    await add_tokens(target_id, int(txt))
    await update.message.reply_text(f"✅ Выдано {txt} токенов пользователю {target_id}", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Админ-диалог отменен.", reply_markup=user_menu)
    return ConversationHandler.END

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("redeem", redeem_cmd))

    # кнопки пользователя
    app.add_handler(MessageHandler(filters.Regex("^💰 Мой баланс$|^➕ Пополнить \\(промокод\\)$|^ℹ️ Помощь$"), on_user_button))

    # админ-меню
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_handler)],
            ASK_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_user_id)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_amount)],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True
    )
    app.add_handler(admin_conv)

    # AI обработка
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Webhook
    port = int(os.environ.get("PORT", 5000))
    webhook_url = f"{RENDER_URL}/webhook/{os.getenv('TELEGRAM_TOKEN')}"
    logger.info(f"Запуск Webhook -> {webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=f"webhook/{os.getenv('TELEGRAM_TOKEN')}",
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
