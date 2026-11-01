import asyncio
from collections import defaultdict
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User

MAX_OPEN_GROUPS = 3
PAGE_SIZE = 5
BILLING_DAY_MIN = 1
BILLING_DAY_MAX = 28
CENT = Decimal("0.01")

SessionMaker = async_sessionmaker[AsyncSession]


# Создание групп одного владельца идёт по очереди: проверка лимита и вставка не должны
# перемешиваться между параллельными запросами (в PostgreSQL дополнительно FOR UPDATE).
_owner_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class GroupError(Exception):
    """Ошибка бизнес-правил; текст безопасно показывать пользователю."""


class GroupLimitError(GroupError):
    pass


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
    slots_total: int,
    slots_free: int,
    billing_day: int,
) -> Group:
    async with _owner_locks[owner_id], session_maker() as session:
        service = await session.get(Service, service_id)
        if service is None or not service.is_active:
            raise GroupError("Этот сервис больше недоступен.")
        # Блокируем строку владельца (на PostgreSQL), чтобы параллельные создания
        # не обошли лимит открытых групп
        await session.get(User, owner_id, with_for_update=True)
        validate_slots_total(slots_total, service.max_members)
        validate_slots_free(slots_free, slots_total)
        validate_billing_day(billing_day)
        if await _count_open_groups(session, owner_id) >= MAX_OPEN_GROUPS:
            raise _limit_error()
        group = Group(
            service_id=service_id,
            owner_id=owner_id,
            price_per_member=calc_price_per_member(service.price_month, slots_total),
            slots_total=slots_total,
            slots_free=slots_free,
            billing_day=billing_day,
            status=GroupStatus.OPEN,
        )
        session.add(group)
        await session.commit()
        return group


async def list_owned_groups(
    session_maker: SessionMaker, owner_id: int
) -> list[tuple[Group, Service]]:
    async with session_maker() as session:
        stmt = (
            select(Group, Service)
            .join(Service, Group.service_id == Service.id)
            .where(Group.owner_id == owner_id)
            .order_by(Group.id.desc())
        )
        return [(g, s) for g, s in (await session.execute(stmt)).all()]


def _visible_to(viewer_id: int) -> list[ColumnElement[bool]]:
    """Условия поиска: открытые группы со свободными местами, кроме своих и тех, где
    пользователь уже состоит, ждёт ответа или получил отказ."""
    already_involved = exists().where(
        Membership.group_id == Group.id,
        Membership.user_id == viewer_id,
        Membership.status.in_(
            (MembershipStatus.PENDING, MembershipStatus.APPROVED, MembershipStatus.REJECTED)
        ),
    )
    return [
        Group.status == GroupStatus.OPEN,
        Group.slots_free > 0,
        Group.owner_id != viewer_id,
        ~already_involved,
    ]


async def list_open_groups(
    session_maker: SessionMaker, service_id: int, viewer_id: int, offset: int, limit: int
) -> list[Group]:
    """Группы сервиса для просмотра: больше свободных мест выше, при равенстве новее выше."""
    stmt = (
        select(Group)
        .where(Group.service_id == service_id, *_visible_to(viewer_id))
        .order_by(Group.slots_free.desc(), Group.created_at.desc(), Group.id.desc())
        .offset(max(offset, 0))
        .limit(limit)
    )
    async with session_maker() as session:
        return list(await session.scalars(stmt))


async def count_open_groups_by_service(
    session_maker: SessionMaker, viewer_id: int
) -> dict[int, int]:
    """Сколько групп видит пользователь по каждому сервису (для списка сервисов)."""
    stmt = (
        select(Group.service_id, func.count())
        .where(*_visible_to(viewer_id))
        .group_by(Group.service_id)
    )
    async with session_maker() as session:
        return {service_id: count for service_id, count in await session.execute(stmt)}
