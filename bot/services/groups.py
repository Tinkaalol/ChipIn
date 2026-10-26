from decimal import ROUND_CEILING, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Group, GroupStatus, Service

MAX_OPEN_GROUPS = 3
MAX_PRICE = Decimal("100000")
BILLING_DAY_MIN = 1
BILLING_DAY_MAX = 28
CENT = Decimal("0.01")

SessionMaker = async_sessionmaker[AsyncSession]


class GroupError(Exception):
    """Ошибка бизнес-правил; текст безопасно показывать пользователю."""


class GroupLimitError(GroupError):
    pass


def parse_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise GroupError("Нужно целое число, например 3.") from None


def parse_price(text: str) -> Decimal:
    raw = text.strip().replace(",", ".").replace("₽", "").strip()
    try:
        price = Decimal(raw)
    except InvalidOperation:
        raise GroupError("Не понял цену. Напишите число, например 299 или 299.50.") from None
    if not price.is_finite() or price <= 0:
        raise GroupError("Цена должна быть больше нуля.")
    if price > MAX_PRICE:
        raise GroupError(f"Слишком большая цена, максимум {MAX_PRICE:.0f}.")
    if price != price.quantize(CENT):
        raise GroupError("У цены не больше двух знаков после запятой.")
    return price.quantize(CENT)


def validate_slots_total(total: int, max_members: int) -> int:
    if total < 2:
        raise GroupError("В группе минимум 2 места: вы и ещё хотя бы один участник.")
    if total > max_members:
        raise GroupError(f"Тариф рассчитан максимум на {max_members} мест, включая вас.")
    return total


def validate_slots_free(free: int, total: int) -> int:
    # Одно место занимает сам организатор
    if free < 1:
        raise GroupError("Свободных мест должно быть хотя бы одно.")
    if free > total - 1:
        raise GroupError(f"Свободных мест не больше {total - 1}: одно занимаете вы.")
    return free


def validate_billing_day(day: int) -> int:
    if not BILLING_DAY_MIN <= day <= BILLING_DAY_MAX:
        raise GroupError(
            f"День оплаты от {BILLING_DAY_MIN} до {BILLING_DAY_MAX}, чтобы он был в любом месяце."
        )
    return day


def calc_price_per_member(price_month: Decimal, slots_total: int) -> Decimal:
    """Цена на одного участника: делим на все места, округляем вверх до копейки."""
    if slots_total < 1:
        raise GroupError("Число мест должно быть положительным.")
    return (price_month / slots_total).quantize(CENT, rounding=ROUND_CEILING)


async def list_active_services(session_maker: SessionMaker) -> list[Service]:
    async with session_maker() as session:
        result = await session.scalars(
            select(Service).where(Service.is_active.is_(True)).order_by(Service.id)
        )
        return list(result)


async def get_service(session_maker: SessionMaker, service_id: int) -> Service | None:
    async with session_maker() as session:
        return await session.get(Service, service_id)


async def _count_open_groups(session: AsyncSession, owner_id: int) -> int:
    # Заполненная группа остаётся активной, поэтому считаем всё, что не закрыто
    stmt = (
        select(func.count())
        .select_from(Group)
        .where(Group.owner_id == owner_id, Group.status != GroupStatus.CLOSED)
    )
    return await session.scalar(stmt) or 0


async def count_open_groups(session_maker: SessionMaker, owner_id: int) -> int:
    async with session_maker() as session:
        return await _count_open_groups(session, owner_id)


def _limit_error() -> GroupLimitError:
    return GroupLimitError(
        f"У вас уже {MAX_OPEN_GROUPS} открытые группы — это максимум. "
        "Закройте одну из них, чтобы создать новую."
    )


async def ensure_can_create(session_maker: SessionMaker, owner_id: int) -> None:
    if await count_open_groups(session_maker, owner_id) >= MAX_OPEN_GROUPS:
        raise _limit_error()


async def create_group(
    session_maker: SessionMaker,
    owner_id: int,
    service_id: int,
    price_month: Decimal,
    slots_total: int,
    slots_free: int,
    billing_day: int,
) -> Group:
    async with session_maker() as session:
        service = await session.get(Service, service_id)
        if service is None or not service.is_active:
            raise GroupError("Этот сервис больше недоступен.")
        validate_slots_total(slots_total, service.max_members)
        validate_slots_free(slots_free, slots_total)
        validate_billing_day(billing_day)
        if price_month <= 0 or price_month > MAX_PRICE:
            raise GroupError("Некорректная цена.")
        if await _count_open_groups(session, owner_id) >= MAX_OPEN_GROUPS:
            raise _limit_error()
        group = Group(
            service_id=service_id,
            owner_id=owner_id,
            price_per_member=calc_price_per_member(price_month, slots_total),
            slots_total=slots_total,
            slots_free=slots_free,
            billing_day=billing_day,
            status=GroupStatus.OPEN,
        )
        session.add(group)
        await session.commit()
        return group
