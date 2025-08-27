# Telegram ChatGPT Bot (PTB 20.7, OpenAI 1.x)

## Быстрый старт
1) Установи зависимости:
   ```bash
   pip install -r requirements.txt
   ```

2) Открой `config.py` и вставь свои ключи:
   ```python
   TELEGRAM_TOKEN = "ваш_токен"
   OPENAI_API_KEY = "ваш_api_key"
   ```

3) Запусти бота:
   ```bash
   python bot.py
   ```

## Заметки
- Код использует **python-telegram-bot 20.7** — без Updater, только `Application`.
- OpenAI SDK: `openai>=1.x`, используется `AsyncOpenAI` и endpoint `chat.completions`.
- Модель по умолчанию: `gpt-4o-mini`. Можно заменить на доступную в вашем аккаунте.
- Если увидите ошибку про ключи — значит, вы не заполнили `config.py`.
