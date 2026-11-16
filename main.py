import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import flow
import handlers
from config import get_settings
from db import connect
from health import start_health_server


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    db = await connect(settings.supabase_url, settings.supabase_key)
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.include_router(flow.router)
    health = await start_health_server()
    try:
        # db и реквизиты попадают в хендлеры как аргументы по имени
        await dp.start_polling(bot, db=db, kaspi_info=settings.kaspi_payment_info)
    finally:
        if health is not None:
            await health.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
