import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.config import get_settings
from bot.handlers import create_group, start


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(create_group.router)
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    bot = Bot(token=settings.bot_token)
    dp = build_dispatcher()
    logging.getLogger(__name__).info("Бот запущен (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
