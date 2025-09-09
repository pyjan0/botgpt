# bot.py
import os
import base64
import json
import random
import logging
from io import BytesIO
from typing import Dict, Any
from google.cloud import firestore

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-gpt-bot")

# ====== CONFIG / ENV ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL")

# список ключей для OpenRouter (через запятую в ENV)
OPENROUTER_KEYS = os.environ.get("OPENROUTER_KEYS", "").split(",")
OPENROUTER_KEYS = [k.strip() for k in OPENROUTER_KEYS if k.strip()]
if not OPENROUTER_KEYS:
    raise RuntimeError("Нет ключей OpenRouter! Укажи их в OPENROUTER_KEYS")

# стоимость запросов
MODEL_COSTS = {
    "gpt-3.5-turbo": 3,
    "gpt-4o-mini": 5,
    "gpt-4o": 7,
}
DEFAULT_MODEL = "gpt-4o"

DAN_PROMPT = "Ты полезный ассистент, который честно и понятно отвечает на вопросы."

# ====== FIREBASE INIT ======
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID")
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

if not firebase_admin._apps:
    sa_json = FIREBASE_SERVICE_ACCOUNT_JSON
    if not sa_json:
        raise RuntimeError("Не задана FIREBASE_SERVICE_ACCOUNT_JSON")

    try:
        if sa_json.strip().startswith("{"):
            sa = json.loads(sa_json)
        else:
            sa = json.loads(base64.b64decode(sa_json).decode("utf-8"))
    except Exception as e:
        raise RuntimeError("Ошибка чтения сервисного аккаунта Firebase") from e

    cred = credentials.Certificate(sa)
    firebase_admin.initialize_app(cred, {'projectId': FIREBASE_PROJECT_ID})
db = firestore.client()

COL_USERS = "users"
COL_PROMOS = "promocodes"

# ====== HELPERS ======
def user_doc_ref(user_id: int):
    return db.collection(COL_USERS).document(str(user_id))

DEFAULT_TOKENS = 60  # хватит на +-9 запросов

def get_user(user_id):
    ref = user_doc_ref(user_id)
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    else:
        # создаём нового пользователя с дефолтным балансом
        user_data = {"tokens": DEFAULT_TOKENS, "memory": []}
        ref.set(user_data)
        return user_data


def update_user(user_id: int, data: Dict[str, Any]):
    user_doc_ref(user_id).set(data, merge=True)


def change_balance(user_id, amount):
    ref = user_doc_ref(user_id)
    transaction = db.transaction()

    @firestore.transactional
    def update_in_transaction(tr):
        doc = ref.get(transaction=tr)
        if doc.exists:
            tokens = doc.get("tokens")
            if tokens is None:
                tokens = DEFAULT_TOKENS
            tokens += amount
            tokens = max(tokens, 0)
            tr.update(ref, {"tokens": tokens})
            return tokens
        else:
            # новый пользователь с дефолтными токенами
            tr.set(ref, {"tokens": max(DEFAULT_TOKENS + amount, 0), "memory": []})
            return max(DEFAULT_TOKENS + amount, 0)

    return update_in_transaction(transaction)


def cost_for_model(model_name: str) -> int:
    return MODEL_COSTS.get(model_name, MODEL_COSTS[DEFAULT_MODEL])

# ====== CHAT WITH AI ======
async def chat_with_ai(user_id: int, message_content) -> str:
    model = get_user(user_id).get("model", DEFAULT_MODEL)
    system_messages = [{"role": "system", "content": DAN_PROMPT}]
    memory = get_user(user_id).get("memory", "")
    if memory:
        system_messages.append({"role": "system", "content": f"Память: {memory}"})

    user_part = {"role": "user", "content": message_content}

    # пробуем ключи по очереди
    for key in random.sample(OPENROUTER_KEYS, len(OPENROUTER_KEYS)):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": system_messages + [user_part],
                    },
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"Ключ {key[:10]}... не сработал: {e}")

    return "⚠️ Все ключи сейчас недоступны. Попробуй позже."

# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот 🤖\n"
        "Команды:\n"
        "/balance — баланс\n"
        "/redeem <код> — ввести промокод\n"
        "/memory — показать память\n"
        "/remember <текст> — сохранить память\n"
        "/clearmemory — очистить память\n"
        "/setmodel <model> — выбрать модель (gpt-3.5-turbo, gpt-4o-mini, gpt-4o)\n"
    )

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    await update.message.reply_text(f"Баланс: {u['balance']} коинов\nМодель: {u['model']}")

async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Использование: /remember <текст>")
        return
    update_user(update.effective_user.id, {"memory": text})
    await update.message.reply_text("Память сохранена ✅")

async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mem = get_user(update.effective_user.id).get("memory", "")
    await update.message.reply_text(mem or "Память пуста.")

async def clearmemory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user(update.effective_user.id, {"memory": ""})
    await update.message.reply_text("Память очищена.")

async def setmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /setmodel gpt-3.5-turbo")
        return
    model = context.args[0]
    if model not in MODEL_COSTS:
        await update.message.reply_text("Недоступная модель.")
        return
    update_user(update.effective_user.id, {"model": model})
    await update.message.reply_text(f"Модель установлена: {model}")

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /redeem <код>")
        return
    code = context.args[0].upper()
    doc_ref = db.collection(COL_PROMOS).document(code)
    doc = doc_ref.get()
    if not doc.exists:
        await update.message.reply_text("Такого промокода нет.")
        return
    data = doc.to_dict()
    if data["uses_left"] <= 0:
        await update.message.reply_text("У промокода закончились активации.")
        return

    def txn_fn(transaction, ref):
        snap = ref.get(transaction=transaction)
        d = snap.to_dict()
        if d["uses_left"] <= 0:
            raise RuntimeError("no_uses")
        transaction.update(ref, {"uses_left": d["uses_left"] - 1})
        return d["amount"]

    try:
        amount = db.run_transaction(lambda tr: txn_fn(tr, doc_ref))
        new_bal = change_balance(update.effective_user.id, amount)
        await update.message.reply_text(f"+{amount} коинов! Баланс: {new_bal}")
    except:
        await update.message.reply_text("Не удалось активировать код.")

# ====== MESSAGE HANDLERS ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    user = get_user(uid)
    model = user["model"]
    cost = cost_for_model(model)

    if user["balance"] < cost:
        await update.message.reply_text(f"Нужно {cost} коинов, у тебя {user['balance']}.")
        return

    new_bal = change_balance(uid, -cost)
    await update.message.reply_text(f"Списано {cost} коинов. Остаток: {new_bal}")
    reply = await chat_with_ai(uid, text)
    await update.message.reply_text(reply)

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("clearmemory", clearmemory_cmd))
    app.add_handler(CommandHandler("setmodel", setmodel_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 5000))
    webhook_url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"

    logger.info(f"Запуск 🚀 Webhook -> {webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=f"webhook/{TELEGRAM_TOKEN}",
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()
