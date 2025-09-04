import logging
import httpx
import os
import base64
import json
from io import BytesIO
from typing import Tuple, Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackContext,
    filters,
)

from config import TELEGRAM_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL

# ========= НАСТРОЙКИ =========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-gpt-bot")

# Админ ID из ENV или вручную
ADMIN_ID = int(os.getenv("ADMIN_ID", "8033358653"))  # укажи свой ID в переменной окружения ADMIN_ID

# Стоимость операций (в токенах)
TEXT_COST = int(os.getenv("TEXT_COST", "1"))
PHOTO_COST = int(os.getenv("PHOTO_COST", "2"))
DOC_COST = int(os.getenv("DOC_COST", "2"))

# Начальный баланс новых пользователей
DEFAULT_TOKENS = int(os.getenv("DEFAULT_TOKENS", "20"))

# Файлы БД
DB_FILE = "users.json"
PROMO_FILE = "promocodes.json"

# Webhook базовый URL (Render)
RENDER_URL = os.getenv("RENDER_URL")

# Инструкция для модели
DAN_PROMPT = """
Ты полезный ассистент, который честно и понятно отвечает на вопросы.
Если вместе с фото есть текстовый вопрос — приоритет отдавай вопросу, а фото используй как контекст.
Если прислан файл с текстом — объясни, что это за файл и что он делает. Будь кратким и по делу.
"""

# ========= ХРАНИЛИЩЕ (JSON) =========
def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception(f"Поврежден файл {path}, будет пересоздан.")
    return {}

def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

users = _load_json(DB_FILE)      # { user_id: {"tokens": int} }
promos = _load_json(PROMO_FILE)  # { "CODE": amount }

def ensure_user(user_id: int) -> None:
    uid = str(user_id)
    if uid not in users:
        users[uid] = {"tokens": DEFAULT_TOKENS}
        _save_json(DB_FILE, users)

def get_tokens(user_id: int) -> int:
    return users.get(str(user_id), {}).get("tokens", DEFAULT_TOKENS)

def add_tokens(user_id: int | str, amount: int) -> None:
    uid = str(user_id)
    ensure_user(int(uid))
    users[uid]["tokens"] = get_tokens(int(uid)) + amount
    _save_json(DB_FILE, users)

def use_tokens(user_id: int, amount: int) -> bool:
    ensure_user(user_id)
    have = get_tokens(user_id)
    if have >= amount:
        users[str(user_id)]["tokens"] = have - amount
        _save_json(DB_FILE, users)
        return True
    return False

def refund_tokens(user_id: int, amount: int) -> None:
    # Возврат токенов при ошибке AI
    add_tokens(user_id, amount)

def create_promo(code: str, amount: int) -> None:
    promos[code] = amount
    _save_json(PROMO_FILE, promos)

def redeem_promo(user_id: int, code: str) -> bool:
    if code in promos:
        add_tokens(user_id, int(promos[code]))
        del promos[code]
        _save_json(PROMO_FILE, promos)
        return True
    return False

# ========= МЕНЮ =========
user_menu = ReplyKeyboardMarkup(
    [
        ["💰 Мой баланс", "➕ Пополнить (промокод)"],
        ["ℹ️ Помощь"],
    ],
    resize_keyboard=True
)

admin_menu = ReplyKeyboardMarkup(
    [
        ["💰 Мой баланс", "➕ Пополнить (промокод)"],
        ["💎 Выдать токены", "🎁 Создать промокод"],
        ["🔙 Назад"],
    ],
    resize_keyboard=True
)

# ========= GPT =========
async def openrouter_chat(messages: list, model: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "messages": messages},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        text = e.response.text
        logger.error(f"Ошибка API {code}: {text}")
        raise
    except Exception as e:
        logger.error(f"Неизвестная ошибка API: {e}")
        raise

async def chat_with_ai_text(user_id: int, message: str) -> str:
    msgs = [
        {"role": "system", "content": DAN_PROMPT},
        {"role": "user", "content": message}
    ]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_image(user_question: str, b64_image: str) -> str:
    user_content = [
        {
            "type": "text",
            "text": (
                f"Вопрос пользователя: {user_question}\n"
                f"Ниже приложено изображение. Используй его как контекст, но отвечай на сам вопрос."
            ),
        },
        {
            "type": "image_url",
            "image_url": f"data:image/jpeg;base64,{b64_image}",
        },
    ]
    msgs = [
        {"role": "system", "content": DAN_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

async def chat_with_ai_file(filename: str, text: str) -> str:
    snippet = text[:8000]  # безопасный лимит
    prompt = (
        f"Пользователь прислал файл: {filename}\n"
        f"Объясни простыми словами, что это за файл, что делает код/содержимое, "
        f"и укажи потенциальные проблемы, если они есть.\n\n"
        f"Содержимое (фрагмент):\n{snippet}"
    )
    msgs = [
        {"role": "system", "content": DAN_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return await openrouter_chat(msgs, OPENROUTER_MODEL)

# ========= ХЕЛПЕРЫ =========
def is_admin(user_id: int) -> bool:
    return ADMIN_ID and user_id == ADMIN_ID

async def send_help(update: Update):
    await update.message.reply_text(
        "ℹ️ Я GPT-бот:\n"
        "— Отвечаю на текстовые вопросы\n"
        "— Анализирую фото с подписью (caption)\n"
        "— Читаю текстовые файлы (.py, .txt, .json) и объясняю их\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/balance — баланс\n"
        "/redeem КОД — применить промокод\n"
        "\n"
    )

# ========= ОБРАБОТЧИКИ /start /balance /redeem =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    kb = admin_menu if is_admin(uid) else user_menu
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

# ========= ПОЛЬЗОВАТЕЛЬСКИЕ КНОПКИ =========
async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    uid = update.effective_user.id
    if txt == "💰 Мой баланс":
        await balance(update, context)
    elif txt == "➕ Пополнить (промокод)":
        await update.message.reply_text("Введите промокод командой: /redeem КОД")
    elif txt == "ℹ️ Помощь":
        await send_help(update)

# ========= ТЕКСТ =========
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    # Пытаемся списать, если не хватает — сообщаем
    if not use_tokens(uid, TEXT_COST):
        await update.message.reply_text("❌ Недостаточно токенов. Введите промокод: /redeem КОД")
        return

    try:
        reply = await chat_with_ai_text(uid, update.message.text)
        await update.message.reply_text(reply)
    except Exception:
        refund_tokens(uid, TEXT_COST)
        await update.message.reply_text("⚠️ Ошибка при обращении к AI. Токены возвращены.")

# ========= ФОТО =========
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

# ========= ФАЙЛЫ =========
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
                # пробуем мягче
                text = file_bytes.decode("utf-8", errors="replace")
            reply = await chat_with_ai_file(filename, text)
            await update.message.reply_text(reply)
        else:
            # Бинарники — просто возвращаем
            await update.message.reply_document(BytesIO(file_bytes), filename=filename)
            await update.message.reply_text("Это бинарный файл — вернул его обратно.")
    except Exception:
        refund_tokens(uid, DOC_COST)
        await update.message.reply_text("⚠️ Ошибка при обработке файла. Токены возвращены.")

# ========= АДМИН-ПАНЕЛЬ (ConversationHandler) =========
(
    ADMIN_MENU,
    ASK_GIVE_ID,
    ASK_GIVE_AMOUNT,
    ASK_PROMO_CODE,
    ASK_PROMO_AMOUNT,
) = range(5)

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔧 Админ-меню", reply_markup=admin_menu)
    return ADMIN_MENU

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    txt = update.message.text.strip()

    if txt == "💎 Выдать токены":
        await update.message.reply_text("Введите USER_ID, кому выдать токены:", reply_markup=ReplyKeyboardRemove())
        return ASK_GIVE_ID

    if txt == "🎁 Создать промокод":
        await update.message.reply_text("Введите текст промокода (например, FREE50):", reply_markup=ReplyKeyboardRemove())
        return ASK_PROMO_CODE

    if txt == "🔙 Назад":
        await update.message.reply_text("Вы вышли из админ-панели.", reply_markup=user_menu)
        return ConversationHandler.END

    # Игнор прочих нажатий
    return ADMIN_MENU

# — Выдать токены: шаг 1 (ID)
async def admin_ask_give_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_txt = update.message.text.strip()
    if not uid_txt.isdigit():
        await update.message.reply_text("Нужен числовой USER_ID. Попробуйте снова или нажмите /cancel.")
        return ASK_GIVE_ID
    context.user_data["give_uid"] = int(uid_txt)
    await update.message.reply_text("Сколько токенов выдать?")
    return ASK_GIVE_AMOUNT

# — Выдать токены: шаг 2 (количество)
async def admin_ask_give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_txt = update.message.text.strip()
    if not amount_txt.lstrip("-").isdigit():
        await update.message.reply_text("Введите целое число. Попробуйте снова или нажмите /cancel.")
        return ASK_GIVE_AMOUNT
    amount = int(amount_txt)
    target_id = context.user_data.get("give_uid")
    add_tokens(target_id, amount)
    await update.message.reply_text(f"✅ Выдано {amount} токенов пользователю {target_id}.", reply_markup=admin_menu)
    context.user_data.pop("give_uid", None)
    return ADMIN_MENU

# — Создать промокод: шаг 1 (код)
async def admin_ask_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    if not code or " " in code:
        await update.message.reply_text("Промокод не должен быть пустым и без пробелов. Попробуйте снова или нажмите /cancel.")
        return ASK_PROMO_CODE
    context.user_data["promo_code"] = code
    await update.message.reply_text("На сколько токенов этот промокод?")
    return ASK_PROMO_AMOUNT

# — Создать промокод: шаг 2 (количество)
async def admin_ask_promo_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_txt = update.message.text.strip()
    if not amount_txt.lstrip("-").isdigit():
        await update.message.reply_text("Введите целое число. Попробуйте снова или нажмите /cancel.")
        return ASK_PROMO_AMOUNT
    amount = int(amount_txt)
    code = context.user_data.get("promo_code")
    create_promo(code, amount)
    await update.message.reply_text(f"✅ Промокод {code} создан на {amount} токенов.", reply_markup=admin_menu)
    context.user_data.pop("promo_code", None)
    return ADMIN_MENU

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Админ-диалог отменён.", reply_markup=admin_menu)
    else:
        await update.message.reply_text("Диалог отменён.", reply_markup=user_menu)
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

    # админ-панель (вход /admin)
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            ADMIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_handler),
            ],
            ASK_GIVE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_give_id),
            ],
            ASK_GIVE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_give_amount),
            ],
            ASK_PROMO_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_promo_code),
            ],
            ASK_PROMO_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ask_promo_amount),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
    )
    app.add_handler(admin_conv)

    # сообщения (как раньше)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
