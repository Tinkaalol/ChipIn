# Проект: Telegram-бот для совместной оплаты подписок

Бот сводит людей, которые хотят вместе оплачивать подписку на онлайн-сервис. Организатор создаёт группу, участники находят её и подают заявку. Деньги бот НЕ принимает и НЕ хранит: участники переводят их друг другу сами. Бот только знакомит и напоминает о дате оплаты.

## Архитектура
Сайт — источник правды, бот — канал уведомлений. Группы, заявки и участников ведёт сайт (backend), вся бизнес-логика живёт в общем пакете `shared/`. Бот использует тот же код и ту же базу, но по сути он канал: присылает уведомления (заявка, решение, удаление, напоминание об оплате) и позволяет быстро выполнить действие. Любое состояние, которое видно в боте, должно быть выводимо из базы и из `shared.services`, а не жить только в боте.
Backend и бот НЕ импортируют друг друга: общий код только через `shared/`.

## Безопасность
- Бот не принимает и не отправляет пароли, данные карт и доступы к сервисам. Это относится и к сайту: такие поля в формах, БД и сообщениях недопустимы.
- Участники сами договариваются об оплате и доступе вне бота.

## Стек
- Python 3.12, aiogram 3 (бот), FastAPI (backend), SQLAlchemy 2 (async) + Alembic (один на весь проект, папка migrations/)
- PostgreSQL (asyncpg) в работе; тесты по умолчанию на SQLite в памяти (aiosqlite), с TEST_DATABASE_URL — на PostgreSQL
- APScheduler для напоминаний, внутри того же процесса
- Конфиг из .env через pydantic-settings
- Тесты: pytest + pytest-asyncio

## Структура
```
shared/        общий код: config.py, db/ (models, session), services/ (groups, memberships, moderation, catalog, users)
backend/       FastAPI: main.py (приложение, JSON API), web.py (страницы), auth.py (проверка Telegram Login),
               deps.py (сессия, CSRF), config.py, templates/ (Jinja2 + HTMX), static/
bot/           aiogram: main.py, config.py (токен), handlers/, keyboards.py, middlewares.py, texts.py, ui.py,
               services/ (reminders, ratelimit: только то, что связано с Telegram)
migrations/    Alembic (env.py берёт модели из shared.db.models)
scripts/       разовые скрипты (seed_services)
tests/         pytest для shared, bot и backend
docker-compose.yml, Dockerfile, .env.example
```

## Команды
- Всё в Docker: `docker compose up --build` (db, migrate, backend :8000, bot)
- Только база: `docker compose up -d db`; миграции: `alembic upgrade head`
- Бот: `python -m bot.main` (разработка с перезапуском: `./dev.sh`); backend: `uvicorn backend.main:app --reload`
- Тесты: `pytest` (SQLite); на PostgreSQL: `TEST_DATABASE_URL=postgresql+asyncpg://... pytest`; линтер: `ruff check .`

## Модель данных
users(id, telegram_id, username, language, is_banned, created_at); services(id, name, plan_name, price_month, currency, max_members, is_active); groups(id, service_id, owner_id, price_per_member, slots_total, slots_free, billing_day, status open/full/closed, created_at); memberships(id, group_id, user_id, status pending/approved/rejected/left/removed, joined_at); reports(id, reporter_id, target_user_id, group_id, reason, status).

## Правила
- Сайт: вход только через Telegram Login Widget с серверной проверкой подписи (backend/auth.py); сессия в подписанном HttpOnly/Secure/SameSite=Lax cookie; каждый POST проверяет CSRF-токен. Доступ к данным проверяется на сервере по роли, шаблоны ничего не решают.
- Интерфейс бота на русском, кнопки inline, у каждого шага диалога есть «Назад» и «Отмена».
- Бизнес-логика только в shared/services (повторно в боте и backend не писать), хендлеры и роуты тонкие.
- Бот никогда не хранит и не передаёт пароли, данные карт и доступы от сервисов.
- Все суммы в тенге (KZT, знак ₸); валюта хранится в services.currency.
- Контакт организатора показывается участнику только после одобрения заявки.
- Лимит: не более 3 открытых групп на пользователя.
- Типы везде, функции короткие, комментарии по-русски только там, где логика неочевидна.
- После каждого этапа запускай тесты и ruff, исправляй ошибки.
