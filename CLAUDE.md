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
shared/        общий код: config.py, dates.py, formatting.py, notifications.py (очередь и тексты), db/ (models, session),
               services/ (groups, memberships, moderation, catalog, users, telegram_link)
backend/       FastAPI: main.py (приложение, JSON API), web.py (страницы), auth.py (проверка Telegram Login),
               deps.py (сессия, CSRF), config.py, templates/ (Jinja2 + HTMX), static/
bot/           aiogram: main.py, config.py (токен), handlers/, keyboards.py, middlewares.py, texts.py, ui.py,
               outbox.py (воркер очереди), services/ (reminders, ratelimit)
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
users(id, telegram_id (null, если Telegram не привязан), username, telegram_username, telegram_linked_at, notify_enabled, language, is_banned, created_at); telegram_link_tokens(id, user_id, token_hash, expires_at, used_at); notification_outbox(id, user_id, type, payload JSON, status pending/sent/failed/skipped, attempts, next_attempt_at, created_at, sent_at, error); services(id, name, plan_name, price_month, currency, max_members, is_active); groups(id, service_id, owner_id, price_per_member, slots_total, slots_free, billing_day, status open/full/closed, created_at); memberships(id, group_id, user_id, status pending/approved/rejected/left/removed, joined_at); reports(id, reporter_id, target_user_id, group_id, reason, status).

## Правила
- Уведомления: никаких прямых bot.send_message из бизнес-логики и хендлеров. Сервис вызывает shared.notifications.enqueue(session, user_id, type, payload) в той же транзакции, что и операция; отправляет очередь воркер bot/outbox.py (повторы с задержкой, 5 попыток). Тексты и кнопки всех типов только в shared/notifications.py; в них нет паролей и данных карт, а где нужно действие, есть ссылка на страницу сайта (нужен https PUBLIC_URL). Нет telegram_id или notify_enabled=false: статус skipped.
- Привязка Telegram: токен одноразовый, живёт 10 минут, в базе только SHA-256. Ссылка https://t.me/<бот>?start=link_<токен>. 
- Сайт: вход только через Telegram Login Widget с серверной проверкой подписи (backend/auth.py); сессия в подписанном HttpOnly/Secure/SameSite=Lax cookie; каждый POST проверяет CSRF-токен. Доступ к данным проверяется на сервере по роли, шаблоны ничего не решают.
- Интерфейс бота на русском, кнопки inline, у каждого шага диалога есть «Назад» и «Отмена».
- Бизнес-логика только в shared/services (повторно в боте и backend не писать), хендлеры и роуты тонкие.
- Бот никогда не хранит и не передаёт пароли, данные карт и доступы от сервисов.
- Все суммы в тенге (KZT, знак ₸); валюта хранится в services.currency.
- Контакт организатора показывается участнику только после одобрения заявки.
- Лимит: не более 3 открытых групп на пользователя.
- Типы везде, функции короткие, комментарии по-русски только там, где логика неочевидна.
- После каждого этапа запускай тесты и ruff, исправляй ошибки.
