# Проект: Telegram-бот для совместной оплаты подписок

Бот сводит людей, которые хотят вместе оплачивать подписку на онлайн-сервис. Организатор создаёт группу, участники находят её и подают заявку. Деньги бот НЕ принимает и НЕ хранит: участники переводят их друг другу сами. Бот только знакомит и напоминает о дате оплаты.

## Стек
- Python 3.12, aiogram 3 (асинхронный), SQLAlchemy 2 (async) + Alembic
- SQLite на старте (aiosqlite), код должен работать и с PostgreSQL без переписывания
- APScheduler для напоминаний, внутри того же процесса
- Конфиг из .env через pydantic-settings
- Тесты: pytest + pytest-asyncio

## Структура
bot/main.py, bot/config.py, bot/keyboards.py, bot/handlers/ (start, search, create_group, my_groups, admin), bot/services/ (groups, reminders), bot/db/ (models, session), migrations/, tests/

## Модель данных
users(id, telegram_id, username, language, is_banned, created_at); services(id, name, plan_name, price_month, currency, max_members, is_active); groups(id, service_id, owner_id, price_per_member, slots_total, slots_free, billing_day, status open/full/closed, created_at); memberships(id, group_id, user_id, status pending/approved/rejected/left, joined_at); reports(id, reporter_id, target_user_id, group_id, reason, status).

## Правила
- Интерфейс бота на русском, кнопки inline, у каждого шага диалога есть «Назад» и «Отмена».
- Бизнес-логика только в bot/services, хендлеры тонкие.
- Бот никогда не хранит и не передаёт пароли от сервисов.
- Контакт организатора показывается участнику только после одобрения заявки.
- Лимит: не более 3 открытых групп на пользователя.
- Типы везде, функции короткие, комментарии по-русски только там, где логика неочевидна.
- После каждого этапа запускай тесты и ruff, исправляй ошибки.
