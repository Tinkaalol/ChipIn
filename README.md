# Бот для совместной оплаты подписок

Telegram-бот, который сводит людей для совместной оплаты подписок. Деньги бот не принимает и не хранит — только знакомит участников и напоминает о дате оплаты.

## Запуск

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # впишите BOT_TOKEN от @BotFather и свой Telegram ID в ADMIN_IDS
python -m bot.main
```

## Проверки

```bash
ruff check .
pytest
```

## Структура

См. [CLAUDE.md](CLAUDE.md): `bot/handlers/` — тонкие хендлеры, `bot/services/` — бизнес-логика, `bot/db/` — модели и сессии, `migrations/` — Alembic.
