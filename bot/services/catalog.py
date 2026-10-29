from decimal import Decimal

from sqlalchemy import select

from bot.db.models import Service
from bot.services.groups import SessionMaker

# ЦЕНЫ ПРИМЕРНЫЕ (руб./мес. за весь тариф), перед запуском в прод сверить с сайтами сервисов.
SERVICES: list[tuple[str, str, str, int]] = [
    ("Spotify", "Family", "299", 6),
    ("YouTube Premium", "Family", "399", 5),
    ("ChatGPT", "Plus", "2000", 2),
    ("Яндекс Плюс", "Мульти", "399", 4),
]


async def seed_services(session_maker: SessionMaker) -> int:
    """Добавляет недостающие сервисы каталога, существующие не трогает."""
    added = 0
    async with session_maker() as session:
        for name, plan, price, max_members in SERVICES:
            exists = await session.scalar(
                select(Service).where(Service.name == name, Service.plan_name == plan)
            )
            if exists is None:
                session.add(
                    Service(
                        name=name,
                        plan_name=plan,
                        price_month=Decimal(price),
                        max_members=max_members,
                    )
                )
                added += 1
        await session.commit()
    return added
