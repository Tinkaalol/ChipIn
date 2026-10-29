import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from alembic import command
from alembic.config import Config

from bot.config import get_settings
from bot.db.session import session_maker
from bot.handlers import create_group, errors, my_groups, start
from bot.services.catalog import seed_services


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(create_group.router)
    dp.include_router(my_groups.router)
    dp.include_router(errors.router)
    return dp


async def prepare_database() -> None:
    """Применяет миграции и дозаполняет каталог сервисов при каждом запуске."""
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    # env.py Alembic сам запускает event loop, поэтому отдельный поток
    await asyncio.to_thread(command.upgrade, config, "head")
    added = await seed_services(session_maker)
    logging.getLogger(__name__).info("База готова, добавлено сервисов: %s", added)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    await prepare_database()
    bot = Bot(token=settings.bot_token)
    dp = build_dispatcher()
    logging.getLogger(__name__).info("Бот запущен (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
