"""Заполняет каталог services вручную. Запуск: python -m scripts.seed_services"""

import asyncio

from bot.db.session import engine, session_maker
from bot.services.catalog import seed_services


async def main() -> None:
    added = await seed_services(session_maker)
    await engine.dispose()
    print(f"Добавлено сервисов: {added}")


if __name__ == "__main__":
    asyncio.run(main())
