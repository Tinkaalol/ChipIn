import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import create
import handlers
import join
from config import get_settings
from db import SchemaError, check_schema, connect
from health import start_health_server


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    try:
        await check_schema(settings.supabase_url, settings.supabase_key)
    except SchemaError as error:
        # Без подходящей таблицы бот всё равно отвечал бы ошибками: лучше остановиться сразу
        logging.getLogger(__name__).error("Supabase не готов: %s", error)
        raise SystemExit(1) from error
    db = await connect(settings.supabase_url, settings.supabase_key)
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    # handlers первым: /start и /my_subs сбрасывают состояние даже посреди заявки
    dp.include_router(handlers.router)
    dp.include_router(create.router)
    dp.include_router(join.router)
    health = await start_health_server()
    try:
        # db попадает в хендлеры как аргумент по имени
        await dp.start_polling(bot, db=db)
    finally:
        if health is not None:
            await health.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
