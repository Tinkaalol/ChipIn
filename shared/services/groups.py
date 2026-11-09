import asyncio
import math
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from shared.notifications import NotificationType, enqueue, user_ref

MAX_OPEN_GROUPS = 3
PAGE_SIZE = 5
REAPPLY_DELAY = timedelta(hours=24)  # пауза перед повторной заявкой после отказа
REMOVED_REAPPLY_DELAY = timedelta(days=7)  # то же после удаления из группы
BILLING_DAY_MIN = 1
BILLING_DAY_MAX = 28
CENT = Decimal("0.01")

SessionMaker = async_sessionmaker[AsyncSession]


# Создание групп одного владельца идёт по очереди: проверка лимита и вставка не должны
# перемешиваться между параллельными запросами (в PostgreSQL дополнительно FOR UPDATE).
_owner_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class Role(StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    PENDING = "pending"
    NONE = "none"


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
    now = datetime.now(UTC)
    cutoff = now - REAPPLY_DELAY
    removed_cutoff = now - REMOVED_REAPPLY_DELAY
    already_involved = exists().where(
        Membership.group_id == Group.id,
        Membership.user_id == viewer_id,
        or_(
            Membership.status.in_((MembershipStatus.PENDING, MembershipStatus.APPROVED)),
            and_(Membership.status == MembershipStatus.REJECTED, Membership.decided_at > cutoff),
            and_(
                Membership.status == MembershipStatus.REMOVED,
                Membership.removed_at > removed_cutoff,
            ),
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


async def role_in_session(session: AsyncSession, group: Group | None, user_id: int) -> Role:
    if group is None:
        return Role.NONE
    if group.owner_id == user_id:
        return Role.OWNER
    status = await session.scalar(
        select(Membership.status).where(
            Membership.group_id == group.id,
            Membership.user_id == user_id,
            Membership.status.in_((MembershipStatus.PENDING, MembershipStatus.APPROVED)),
        )
    )
    if status == MembershipStatus.APPROVED:
        return Role.MEMBER
    if status == MembershipStatus.PENDING:
        return Role.PENDING
    return Role.NONE


async def get_user_role(session_maker: SessionMaker, group_id: int, user_id: int) -> Role:
    """Роль пользователя в группе: owner, member (принят), pending (ждёт ответа) или none."""
    async with session_maker() as session:
        return await role_in_session(session, await session.get(Group, group_id), user_id)


@dataclass
class Application:
    membership: Membership
    group: Group
    service: Service
    applicant: User
    owner: User


@dataclass
class Decision:
    membership: Membership
    group: Group
    service: Service
    member: User
    owner: User
    approved: bool


def _as_utc(moment: datetime) -> datetime:
    # SQLite отдаёт время без пояса, хотя мы всегда пишем UTC
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def request_join(
    session_maker: SessionMaker, group_id: int, applicant_id: int
) -> Application:
    """Создаёт заявку (pending) от пользователя в группу."""
    async with session_maker() as session, session.begin():
        group = await session.get(Group, group_id)
        if group is None:
            raise GroupError("Группа не найдена.")
        if group.status == GroupStatus.CLOSED:
            raise GroupError("Эта группа закрыта.")
        if group.status == GroupStatus.FULL or group.slots_free < 1:
            raise GroupError("В этой группе нет свободных мест.")
        applicant = await session.get(User, applicant_id)
        if applicant is None or applicant.is_banned:
            raise GroupError("Вам недоступна подача заявок.")
        if group.owner_id == applicant_id:
            raise GroupError("Это ваша собственная группа.")
        active = await session.scalar(
            select(Membership.status).where(
                Membership.group_id == group_id,
                Membership.user_id == applicant_id,
                Membership.status.in_((MembershipStatus.PENDING, MembershipStatus.APPROVED)),
            )
        )
        if active == MembershipStatus.APPROVED:
            raise GroupError("Вы уже участник этой группы.")
        if active == MembershipStatus.PENDING:
            raise GroupError("Вы уже подали заявку, ждите ответа организатора.")
        await _check_reapply_delay(session, group_id, applicant_id)
        await _check_removal_delay(session, group_id, applicant_id)
        membership = Membership(group_id=group_id, user_id=applicant_id)
        session.add(membership)
        try:
            await session.flush()
        except IntegrityError:
            # Параллельный двойной клик: уникальный индекс не дал создать вторую заявку
            raise GroupError("Вы уже подали заявку, ждите ответа организатора.") from None
        owner = await session.get(User, group.owner_id)
        service = await session.get(Service, group.service_id)
        await enqueue(
            session,
            group.owner_id,
            NotificationType.NEW_JOIN_REQUEST,
            {
                "group_id": group.id,
                "membership_id": membership.id,
                "title": f"{service.name} {service.plan_name}",  # type: ignore[union-attr]
                "slots_free": group.slots_free,
                **user_ref(applicant, "applicant"),
            },
        )
        return Application(membership, group, service, applicant, owner)  # type: ignore[arg-type]


async def _check_reapply_delay(session: AsyncSession, group_id: int, applicant_id: int) -> None:
    rejected_at = await session.scalar(
        select(func.max(Membership.decided_at)).where(
            Membership.group_id == group_id,
            Membership.user_id == applicant_id,
            Membership.status == MembershipStatus.REJECTED,
        )
    )
    if rejected_at is None:
        return
    wait = _as_utc(rejected_at) + REAPPLY_DELAY - datetime.now(UTC)
    if wait > timedelta(0):
        hours = math.ceil(wait.total_seconds() / 3600)
        raise GroupError(f"Организатор отклонил вашу заявку. Подать новую можно через {hours} ч.")


async def _check_removal_delay(session: AsyncSession, group_id: int, applicant_id: int) -> None:
    removed_at = await session.scalar(
        select(func.max(Membership.removed_at)).where(
            Membership.group_id == group_id,
            Membership.user_id == applicant_id,
            Membership.status == MembershipStatus.REMOVED,
        )
    )
    if removed_at is None:
        return
    wait = _as_utc(removed_at) + REMOVED_REAPPLY_DELAY - datetime.now(UTC)
    if wait > timedelta(0):
        days = math.ceil(wait.total_seconds() / 86400)
        raise GroupError(
            f"Организатор удалил вас из этой группы. Подать заявку снова можно через {days} дн."
        )


async def lock_group(session: AsyncSession, group_id: int) -> Group | None:
    """Блокирует строку группы до конца транзакции.

    PostgreSQL: SELECT ... FOR UPDATE. SQLite его не поддерживает, поэтому там берём
    блокировку записи на всю базу (BEGIN IMMEDIATE), и вторая транзакция ждёт первую.
    """
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        with suppress(OperationalError):  # уже внутри транзакции: хватит условных UPDATE ниже
            await session.execute(text("BEGIN IMMEDIATE"))
    return await session.get(Group, group_id, with_for_update=True, populate_existing=True)


async def _decide(
    session_maker: SessionMaker, membership_id: int, actor_id: int, approve: bool
) -> Decision:
    async with session_maker() as session, session.begin():
        membership = await session.get(Membership, membership_id)
        if membership is None:
            raise GroupError("Заявка не найдена.")
        group = await lock_group(session, membership.group_id)
        if group is None or group.owner_id != actor_id:
            raise GroupError("Это не ваша группа.")
        await session.refresh(membership)  # статус после получения блокировки, а не до неё
        if membership.status != MembershipStatus.PENDING:
            raise GroupError("Эта заявка уже обработана.")
        if approve:
            await _take_slot(session, group.id)
        # Условный UPDATE — вторая линия защиты: если заявку всё же изменил параллельный
        # запрос, rowcount будет 0, и исключение откатит занятое место
        now = datetime.now(UTC)
        result = await session.execute(
            update(Membership)
            .where(Membership.id == membership_id, Membership.status == MembershipStatus.PENDING)
            .values(
                status=MembershipStatus.APPROVED if approve else MembershipStatus.REJECTED,
                joined_at=now if approve else None,
                decided_at=now,
            )
        )
        if result.rowcount == 0:
            raise GroupError("Эта заявка уже обработана.")
        await session.refresh(group)
        await session.refresh(membership)
        if group.slots_free == 0 and group.status == GroupStatus.OPEN:
            group.status = GroupStatus.FULL
        member = await session.get(User, membership.user_id)
        owner = await session.get(User, group.owner_id)
        service = await session.get(Service, group.service_id)
        await _announce_decision(session, group, service, member, owner, approve)  # type: ignore[arg-type]
        return Decision(membership, group, service, member, owner, approve)  # type: ignore[arg-type]


async def _announce_decision(
    session: AsyncSession, group: Group, service: Service, member: User, owner: User, approve: bool
) -> None:
    payload: dict[str, object] = {
        "group_id": group.id,
        "title": f"{service.name} {service.plan_name}",
    }
    if approve:
        payload |= {
            "billing_day": group.billing_day,
            "price": str(group.price_per_member),
            "currency": service.currency,
            **user_ref(owner, "owner"),
        }
    kind = NotificationType.REQUEST_APPROVED if approve else NotificationType.REQUEST_REJECTED
    await enqueue(session, member.id, kind, payload)


async def _take_slot(session: AsyncSession, group_id: int) -> None:
    # Атомарное «уменьшить, если есть что уменьшать»: два одобрения на последнее место
    # не пройдут оба, даже если блокировка строки по какой-то причине не сработала
    result = await session.execute(
        update(Group)
        .where(Group.id == group_id, Group.status == GroupStatus.OPEN, Group.slots_free > 0)
        .values(slots_free=Group.slots_free - 1)
    )
    if result.rowcount == 0:
        raise GroupError("Свободных мест не осталось, заявку принять нельзя.")


async def approve_member(
    session_maker: SessionMaker, membership_id: int, owner_id: int
) -> Decision:
    """Принимает заявку: занимает место, при нуле свободных мест группа становится full."""
    return await _decide(session_maker, membership_id, owner_id, approve=True)


async def reject_member(session_maker: SessionMaker, membership_id: int, owner_id: int) -> Decision:
    """Отклоняет заявку; повторно подать её можно через REAPPLY_DELAY."""
    return await _decide(session_maker, membership_id, owner_id, approve=False)
