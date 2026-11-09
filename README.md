# Бот для совместной оплаты подписок

Telegram-бот, который сводит людей для совместной оплаты подписок. Деньги бот не принимает и не хранит — только знакомит участников и напоминает о дате оплаты.

## Запуск

Всё сразу в Docker (PostgreSQL, миграции, backend на :8000, бот):

```bash
cp .env.example .env   # впишите BOT_TOKEN от @BotFather и свой Telegram ID в ADMIN_IDS
docker compose up --build
```

Локально, без Docker для бота и backend:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d db          # PostgreSQL
alembic upgrade head             # одни миграции на весь проект
python -m bot.main               # бот
uvicorn backend.main:app --reload  # backend
```

Для быстрых опытов можно указать `DATABASE_URL=sqlite+aiosqlite:///./bot.db`.

## Сайт

Страницы: главная, вход, личный кабинет, список и карточка групп, создание группы, настройки.
Вход через Telegram Login Widget, поэтому нужны `BOT_USERNAME` и домен, привязанный к боту
(@BotFather → `/setdomain`), а виджет работает только на https. Для локальной проверки по http
поставьте `COOKIE_SECURE=false`. Задайте также `SESSION_SECRET`.

### Привязка Telegram

В «Настройках» кнопка «Привязать Telegram» выдаёт одноразовую ссылку на бота (10 минут). Для неё нужен `BOT_USERNAME`.
Бот с `PUBLIC_URL` (https) после привязки показывает кнопку «Открыть сайт».

### Уведомления

Бизнес-операции кладут уведомления в таблицу `notification_outbox`, а бот раз в несколько секунд отправляет их
с повторами. Поэтому уведомления работают и для действий, сделанных на сайте. Для кнопок-ссылок на сайт нужен
https-адрес в `PUBLIC_URL`. Бот должен работать постоянно: без него очередь копится и уходит после запуска.

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

Бот перезапускается при каждом изменении `.py` файлов в `bot/`, `shared/` и `migrations/`. Остановка — Ctrl+C.

## Проверки

```bash
ruff check .
pytest                                  # SQLite в памяти
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test pytest   # PostgreSQL
```

## Структура

См. [CLAUDE.md](CLAUDE.md): `shared/` — модели и бизнес-логика групп (общий код), `bot/` — Telegram-бот, `backend/` — FastAPI, `migrations/` — Alembic.
