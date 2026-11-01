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

## Деплой на Render

Лучше всего подходит тип **Background Worker** (порт не нужен), команда запуска `python -m bot.main`.
Если сервис создан как Web Service, бот сам откроет порт из `$PORT` (отвечает `ok` на `/`).
Проще всего: New → Blueprint, указать репозиторий, [render.yaml](render.yaml) создаст воркер и базу PostgreSQL.
Осталось вписать `BOT_TOKEN` и `ADMIN_IDS`. Диск Render не постоянный, поэтому для групп нужна PostgreSQL. Адреса `postgres://` бот сам превращает в `postgresql+asyncpg://`.
Бесплатная база Render удаляется через 30 дней, для постоянной работы возьмите платный тариф.

## Администрирование

Администраторы задаются в `ADMIN_IDS` (Telegram ID через запятую). Команды доступны только им:
`/reports` (жалобы), `/ban @username`, `/unban @username`, `/services` (каталог: добавить, выключить, цена).

## Режим разработки

```bash
./dev.sh
```

Бот перезапускается при каждом изменении `.py` файлов в `bot/` и `migrations/`. Остановка — Ctrl+C.

## Проверки

```bash
ruff check .
pytest
```

## Структура

См. [CLAUDE.md](CLAUDE.md): `bot/handlers/` — тонкие хендлеры, `bot/services/` — бизнес-логика, `bot/db/` — модели и сессии, `migrations/` — Alembic.
