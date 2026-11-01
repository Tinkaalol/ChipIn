from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from bot.db.models import Service
from bot.services.groups import GroupError, SessionMaker

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


MAX_PRICE = Decimal("100000")
MAX_MEMBERS_LIMIT = 20
CENT = Decimal("0.01")


def parse_price(text: str) -> Decimal:
    try:
        price = Decimal(text.strip().replace(",", ".").replace("₽", "").strip())
    except InvalidOperation:
        raise GroupError("Не понял цену. Напишите число, например 299 или 299.50.") from None
    if not price.is_finite() or price <= 0 or price > MAX_PRICE:
        raise GroupError(f"Цена должна быть больше 0 и не больше {MAX_PRICE:.0f}.")
    if price != price.quantize(CENT):
        raise GroupError("У цены не больше двух знаков после запятой.")
    return price.quantize(CENT)


async def list_all_services(session_maker: SessionMaker) -> list[Service]:
    async with session_maker() as session:
        return list(await session.scalars(select(Service).order_by(Service.id)))


async def add_service(
    session_maker: SessionMaker, name: str, plan_name: str, price_month: Decimal, max_members: int
) -> Service:
    name, plan_name = name.strip(), plan_name.strip()
    if not name or not plan_name:
        raise GroupError("Название и тариф не могут быть пустыми.")
    if not 2 <= max_members <= MAX_MEMBERS_LIMIT:
        raise GroupError(f"Максимум участников от 2 до {MAX_MEMBERS_LIMIT}.")
    async with session_maker() as session, session.begin():
        exists = await session.scalar(
            select(Service.id).where(Service.name == name, Service.plan_name == plan_name)
        )
        if exists is not None:
            raise GroupError("Такой сервис с этим тарифом уже есть.")
        service = Service(
            name=name, plan_name=plan_name, price_month=price_month, max_members=max_members
        )
        session.add(service)
        await session.flush()
        return service


async def set_service_active(session_maker: SessionMaker, service_id: int, active: bool) -> Service:
    async with session_maker() as session, session.begin():
        service = await session.get(Service, service_id)
        if service is None:
            raise GroupError("Сервис не найден.")
        service.is_active = active
        return service


async def set_service_price(
    session_maker: SessionMaker, service_id: int, price_month: Decimal
) -> Service:
    """Новая цена действует для групп, созданных после изменения."""
    async with session_maker() as session, session.begin():
        service = await session.get(Service, service_id)
        if service is None:
            raise GroupError("Сервис не найден.")
        service.price_month = price_month
        return service
