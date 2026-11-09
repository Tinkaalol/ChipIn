import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from alembic import command
from alembic.config import Config

from bot.config import get_settings
from bot.handlers import admin, create_group, errors, my_groups, report, search, start
from bot.health import start_health_server
from bot.middlewares import BanMiddleware, TextStateGuard
from bot.outbox import SHUTDOWN_TIMEOUT, run_outbox
from bot.services.reminders import start_reminders
from shared.db.session import engine, session_maker
from shared.services.catalog import seed_services
from shared.services.groups import SessionMaker


def build_dispatcher(sm: SessionMaker = session_maker) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(BanMiddleware(sm))
    dp.callback_query.outer_middleware(TextStateGuard())
    dp.include_router(start.router)
    dp.include_router(create_group.router)
    dp.include_router(my_groups.router)
    dp.include_router(search.router)
    dp.include_router(report.router)
    dp.include_router(admin.router)
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
    health = await start_health_server()
    scheduler = start_reminders(session_maker)
    stop_outbox = asyncio.Event()
    outbox = asyncio.create_task(run_outbox(bot, session_maker, settings.public_url, stop_outbox))
    try:
        # Сессию бота закрываем сами: воркеру нужно доотправить текущую пачку
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        stop_outbox.set()
        try:
            await asyncio.wait_for(outbox, SHUTDOWN_TIMEOUT)
        except TimeoutError:
            logging.getLogger(__name__).error("Воркер уведомлений не успел завершиться")
        await bot.session.close()
        scheduler.shutdown(wait=False)
        if health is not None:
            await health.cleanup()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
