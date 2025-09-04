import logging
import os
import base64
from io import BytesIO

import httpx
import firebase_admin
from firebase_admin import credentials, db
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from config import TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL, FIREBASE_CRED_JSON

# ========= ЛОГИ =========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-gpt-bot")

# ========= Настройки =========
ADMIN_ID = int(os.getenv("ADMIN_ID", "8033358653"))
TEXT_COST = int(os.getenv("TEXT_COST", "1"))
PHOTO_COST = int(os.getenv("PHOTO_COST", "2"))
DOC_COST = int(os.getenv("DOC_COST", "2"))
DEFAULT_TOKENS = int(os.getenv("DEFAULT_TOKENS", "20"))

# ========= Firebase =========
cred = credentials.Certificate(FIREBASE_CRED_JSON)
firebase_admin.initialize_app(cred, {
    "databaseURL": "https://botgpttok-default-rtdb.europe-west1.firebasedatabase.app/"
})
users_ref = db.reference("users")
promos_ref = db.reference("promos")
history_ref = db.reference("history")  # опционально для логов операций

# ========= GPT =========
DAN_PROMPT = """Ты полезный ассистент, который честно и понятно отвечает на вопросы."""

async def openrouter_chat(messages: list, model: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка GPT API: {e}")
        raise

async def chat_with_ai_text(user_id: int, message: str) -> str:
    msgs = [
        {"role": "system", "content": DAN_PROMPT},
        {"role": "user", "content": message}
    ]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_image(user_question: str, b64_image: str) -> str:
    user_content = [
        {"type": "text", "text": f"Вопрос пользователя: {user_question}\nНиже приложено изображение. Используй его как контекст."},
        {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64_image}"}
    ]
    msgs = [
        {"role": "system", "content": DAN_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_file(filename: str, text: str) -> str:
    snippet = text[:8000]
    prompt = f"Пользователь прислал файл: {filename}\nОбъясни, что это за файл и его содержание.\nФрагмент:\n{snippet}"
    msgs = [{"role": "system", "content": DAN_PROMPT}, {"role": "user", "content": prompt}]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

# ========= ХЕЛПЕРЫ Firebase =========
def ensure_user(user_id: int):
    uid = str(user_id)
    if not users_ref.child(uid).get():
        users_ref.child(uid).set({"tokens": DEFAULT_TOKENS})

def get_tokens(user_id: int) -> int:
    uid = str(user_id)
    data = users_ref.child(uid).get()
    return data.get("tokens", DEFAULT_TOKENS) if data else DEFAULT_TOKENS

def add_tokens(user_id: int, amount: int):
    uid = str(user_id)
    ensure_user(user_id)
    tokens = get_tokens(user_id) + amount
    users_ref.child(uid).update({"tokens": tokens})

def use_tokens(user_id: int, amount: int) -> bool:
    uid = str(user_id)
    ensure_user(user_id)
    tokens = get_tokens(user_id)
    if tokens >= amount:
        users_ref.child(uid).update({"tokens": tokens - amount})
        return True
    return False

def refund_tokens(user_id: int, amount: int):
    add_tokens(user_id, amount)

def create_promo(code: str, amount: int):
    promos_ref.child(code).set(amount)

def redeem_promo(user_id: int, code: str) -> bool:
    if promos_ref.child(code).get():
        add_tokens(user_id, promos_ref.child(code).get())
        promos_ref.child(code).delete()
        return True
    return False

# ========= МЕНЮ =========
user_menu = ReplyKeyboardMarkup([["💰 Мой баланс", "➕ Пополнить (промокод)"], ["ℹ️ Помощь"]], resize_keyboard=True)
admin_menu = ReplyKeyboardMarkup([["💰 Мой баланс", "➕ Пополнить (промокод)"], ["💎 Выдать токены", "🎁 Создать промокод"], ["🔙 Назад"]], resize_keyboard=True)

# ========= ОБРАБОТЧИКИ =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    kb = admin_menu if uid == ADMIN_ID else user_menu
    await update.message.reply_text("Привет! Я GPT-бот 🤖", reply_markup=kb)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    await update.message.reply_text(f"💰 Ваш баланс: {get_tokens(uid)} токенов.")

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Введите промокод так: /redeem КОД")
        return
    code = context.args[0].strip()
    if redeem_promo(uid, code):
        await update.message.reply_text("✅ Промокод применён! Баланс пополнен.")
    else:
        await update.message.reply_text("❌ Неверный или уже использованный промокод.")

# Пользовательские кнопки
async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    uid = update.effective_user.id
    if txt == "💰 Мой баланс":
        await balance(update, context)
    elif txt == "➕ Пополнить (промокод)":
        await update.message.reply_text("Введите промокод командой: /redeem КОД")
    elif txt == "ℹ️ Помощь":
        await update.message.reply_text("ℹ️ Я GPT-бот:\nОтвечаю на вопросы, анализирую файлы и фото.\nКоманды: /start, /balance, /redeem КОД")

# Текст
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if not use_tokens(uid, TEXT_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    try:
        reply = await chat_with_ai_text(uid, update.message.text)
        await update.message.reply_text(reply)
    except Exception:
        refund_tokens(uid, TEXT_COST)
        await update.message.reply_text("⚠️ Ошибка при обращении к AI. Токены возвращены.")

# Фото
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if not use_tokens(uid, PHOTO_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    try:
        file = await update.message.photo[-1].get_file()
        file_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        question = (update.message.caption or "Что изображено на фото?").strip()
        reply = await chat_with_ai_image(question, b64)
        await update.message.reply_text(reply)
    except Exception:
        refund_tokens(uid, PHOTO_COST)
        await update.message.reply_text("⚠️ Ошибка при обработке изображения. Токены возвращены.")

# Файлы
TEXT_LIKE = {".txt", ".py", ".json", ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv"}

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if not use_tokens(uid, DOC_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return
    doc = update.message.document
    filename = doc.file_name or "file"
    ext = os.path.splitext(filename.lower())[1]
    try:
        tgfile = await doc.get_file()
        file_bytes = await tgfile.download_as_bytearray()
        if ext in TEXT_LIKE:
            try:
                text = file_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                text = file_bytes.decode("utf-8", errors="replace")
            reply = await chat_with_ai_file(filename, text)
            await update.message.reply_text(reply)
        else:
            await update.message.reply_document(BytesIO(file_bytes), filename=filename)
            await update.message.reply_text("Это бинарный файл — вернул его обратно.")
    except Exception:
        refund_tokens(uid, DOC_COST)
        await update.message.reply_text("⚠️ Ошибка при обработке файла. Токены возвращены.")

# ========= АДМИН =========
(ADMIN_MENU, ASK_GIVE_ID, ASK_GIVE_AMOUNT, ASK_PROMO_CODE, ASK_PROMO_AMOUNT) = range(5)

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔧 Админ-меню", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    txt = update.message.text.strip()
    if txt == "💎 Выдать токены":
        await update.message.reply_text("Введите USER_ID:", reply_markup=ReplyKeyboardRemove())
        return ASK_GIVE_ID
    elif txt == "🎁 Создать промокод":
        await update.message.reply_text("Введите текст промокода:", reply_markup=ReplyKeyboardRemove())
        return ASK_PROMO_CODE
    elif txt == "🔙 Назад":
        await update.message.reply_text("Вы вышли из админ-панели.", reply_markup=user_menu)
        return ConversationHandler.END
    return ADMIN_MENU

async def admin_ask_give_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_txt = update.message.text.strip()
    if not uid_txt.isdigit():
        await update.message.reply_text("Нужен числовой USER_ID.")
        return ASK_GIVE_ID
    context.user_data["give_uid"] = int(uid_txt)
    await update.message.reply_text("Сколько токенов выдать?")
    return ASK_GIVE_AMOUNT

async def admin_ask_give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_txt = update.message.text.strip()
    if not amount_txt.lstrip("-").isdigit():
        await update.message.reply_text("Введите число.")
        return ASK_GIVE_AMOUNT
    amount = int(amount_txt)
    target_id = context.user_data.pop("give_uid", None)
    add_tokens(target_id, amount)
    await update.message.reply_text(f"✅ Выдано {amount} токенов пользователю {target_id}.", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_ask_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    context.user_data["promo_code"] = code
    await update.message.reply_text("На сколько токенов этот промокод?")
    return ASK_PROMO_AMOUNT

async def admin_ask_promo_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_txt = update.message.text.strip()
    amount = int(amount_txt)
    code = context.user_data.pop("promo_code", None)
    create_promo(code, amount)
    await update.message.reply_text(f"✅ Промокод {code} создан на {amount} токенов.", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Админ-диалог отменён.", reply_markup=admin_menu)
    return ConversationHandler.END

# ========= MAIN =========
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("redeem", redeem_cmd))

    # пользовательские кнопки
    app.add_handler(MessageHandler(filters.Regex("^💰 Мой баланс$|^➕ Пополнить \\(промокод\\)$|^ℹ️ Помощь$"), on_user_button))

    # админ
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_handler)],
            ASK_GIVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_give_id)],
            ASK_GIVE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_give_amount)],
            ASK_PROMO_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_promo_code)],
            ASK_PROMO_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_promo_amount)],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
    )
    app.add_handler(admin_conv)

    # обработка сообщений
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен 🚀 Polling")
    app.run_polling()

if __name__ == "__main__":
    main()
