from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from bot.services.groups import GroupError, SessionMaker

PAGE_SIZE = 5
ACTIVE = (MembershipStatus.PENDING, MembershipStatus.APPROVED)


@dataclass
class GroupPage:
    groups: list[Group]
    page: int
    pages: int


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


@dataclass
class Departure:
    group: Group
    service: Service
    user: User
    owner: User
    was_approved: bool


@dataclass
class Closure:
    group: Group
    service: Service
    notify_ids: list[int]


@dataclass
class GroupEntry:
    group: Group
    service: Service
    role: str  # "owner" или "member"
    membership_status: MembershipStatus | None


@dataclass
class GroupView:
    group: Group
    service: Service
    role: str
    membership_status: MembershipStatus | None
    # Контакт организатора виден только участнику с одобренной заявкой
    owner_contact: User | None


async def search_groups(
    session_maker: SessionMaker, service_id: int, viewer_id: int, page: int
) -> GroupPage:
    """Открытые группы сервиса со свободными местами, без групп самого пользователя."""
    cond = (
        Group.service_id == service_id,
        Group.status == GroupStatus.OPEN,
        Group.slots_free > 0,
        Group.owner_id != viewer_id,
    )
    async with session_maker() as session:
        total = await session.scalar(select(func.count()).select_from(Group).where(*cond)) or 0
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        stmt = select(Group).where(*cond).order_by(Group.id.desc())
        rows = await session.scalars(stmt.offset(page * PAGE_SIZE).limit(PAGE_SIZE))
        return GroupPage(list(rows), page, pages)


async def get_open_group(
    session_maker: SessionMaker, group_id: int
) -> tuple[Group, Service] | None:
    async with session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.status != GroupStatus.OPEN or group.slots_free < 1:
            return None
        return group, await session.get(Service, group.service_id)  # type: ignore[return-value]


async def apply_to_group(
    session_maker: SessionMaker, group_id: int, applicant_id: int
) -> Application:
    async with session_maker() as session, session.begin():
        group = await session.get(Group, group_id)
        if group is None or group.status != GroupStatus.OPEN or group.slots_free < 1:
            raise GroupError("К сожалению, в этой группе уже нет мест.")
        applicant = await session.get(User, applicant_id)
        if applicant is None or applicant.is_banned:
            raise GroupError("Вам недоступна подача заявок.")
        if group.owner_id == applicant_id:
            raise GroupError("Это ваша собственная группа.")
        existing = await session.scalar(
            select(Membership).where(
                Membership.group_id == group_id,
                Membership.user_id == applicant_id,
                Membership.status.in_((*ACTIVE, MembershipStatus.REJECTED)),
            )
        )
        if existing is not None:
            raise GroupError(
                {
                    MembershipStatus.PENDING: "Вы уже подали заявку, ждите ответа организатора.",
                    MembershipStatus.APPROVED: "Вы уже участник этой группы.",
                    MembershipStatus.REJECTED: "Организатор отклонил вашу заявку в эту группу.",
                }[existing.status]
            )
        membership = Membership(group_id=group_id, user_id=applicant_id)
        session.add(membership)
        try:
            await session.flush()
        except IntegrityError:
            # Параллельный двойной клик: уникальный индекс не дал создать вторую заявку
            raise GroupError("Вы уже подали заявку, ждите ответа организатора.") from None
        owner = await session.get(User, group.owner_id)
        service = await session.get(Service, group.service_id)
        return Application(membership, group, service, applicant, owner)  # type: ignore[arg-type]


async def decide_application(
    session_maker: SessionMaker, membership_id: int, actor_id: int, approve: bool
) -> Decision:
    """Принять или отклонить заявку. Принятие и занятие места — одна транзакция."""
    async with session_maker() as session, session.begin():
        membership = await session.get(Membership, membership_id)
        if membership is None:
            raise GroupError("Заявка не найдена.")
        group = await session.get(Group, membership.group_id)
        if group is None or group.owner_id != actor_id:
            raise GroupError("Это не ваша группа.")
        if membership.status != MembershipStatus.PENDING:
            raise GroupError("Эта заявка уже обработана.")
        if approve:
            await _take_slot(session, group.id)
        # Условный UPDATE: если заявку успел изменить параллельный запрос, rowcount будет 0,
        # исключение откатит и занятое место
        new_status = MembershipStatus.APPROVED if approve else MembershipStatus.REJECTED
        result = await session.execute(
            update(Membership)
            .where(Membership.id == membership_id, Membership.status == MembershipStatus.PENDING)
            .values(status=new_status, joined_at=datetime.now(UTC) if approve else None)
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
        return Decision(membership, group, service, member, owner, approve)  # type: ignore[arg-type]


async def _take_slot(session: AsyncSession, group_id: int) -> None:
    # Атомарное «уменьшить, если есть что уменьшать»: два одобрения на последнее место
    # не пройдут оба, потому что второй UPDATE уже не найдёт slots_free > 0
    result = await session.execute(
        update(Group)
        .where(Group.id == group_id, Group.status == GroupStatus.OPEN, Group.slots_free > 0)
        .values(slots_free=Group.slots_free - 1)
    )
    if result.rowcount == 0:
        raise GroupError("Свободных мест не осталось, заявку принять нельзя.")


async def release_membership(session: AsyncSession, membership: Membership) -> Group:
    """Помечает участие как LEFT; принятому участнику возвращает место в группе."""
    was_approved = membership.status == MembershipStatus.APPROVED
    result = await session.execute(
        update(Membership)
        .where(Membership.id == membership.id, Membership.status == membership.status)
        .values(status=MembershipStatus.LEFT)
    )
    if result.rowcount == 0:
        raise GroupError("Статус участия уже изменился, попробуйте ещё раз.")
    group = await session.get(Group, membership.group_id)
    if was_approved:
        await session.execute(
            update(Group)
            .where(Group.id == group.id, Group.slots_free < Group.slots_total - 1)
            .values(slots_free=Group.slots_free + 1)
        )
        await session.refresh(group)
        if group.status == GroupStatus.FULL:
            group.status = GroupStatus.OPEN
    return group  # type: ignore[return-value]


async def leave_group(session_maker: SessionMaker, group_id: int, user_id: int) -> Departure:
    """Выход участника или отзыв заявки. Место возвращается, если участник был принят."""
    async with session_maker() as session, session.begin():
        membership = await session.scalar(
            select(Membership).where(
                Membership.group_id == group_id,
                Membership.user_id == user_id,
                Membership.status.in_(ACTIVE),
            )
        )
        if membership is None:
            raise GroupError("Вы не состоите в этой группе.")
        was_approved = membership.status == MembershipStatus.APPROVED
        group = await release_membership(session, membership)
        user = await session.get(User, user_id)
        owner = await session.get(User, group.owner_id)
        service = await session.get(Service, group.service_id)
        return Departure(group, service, user, owner, was_approved)  # type: ignore[arg-type]


async def close_in_session(session: AsyncSession, group: Group) -> list[int]:
    """Закрывает группу, отклоняет ждущие заявки; возвращает telegram_id тех, кого уведомить."""
    rows = await session.execute(
        select(User.telegram_id)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.group_id == group.id, Membership.status.in_(ACTIVE))
    )
    notify_ids = [r[0] for r in rows]
    await session.execute(
        update(Membership)
        .where(Membership.group_id == group.id, Membership.status == MembershipStatus.PENDING)
        .values(status=MembershipStatus.REJECTED)
    )
    group.status = GroupStatus.CLOSED
    return notify_ids


async def close_group(session_maker: SessionMaker, group_id: int, owner_id: int) -> Closure:
    async with session_maker() as session, session.begin():
        group = await session.get(Group, group_id)
        if group is None or group.owner_id != owner_id:
            raise GroupError("Это не ваша группа.")
        if group.status == GroupStatus.CLOSED:
            raise GroupError("Группа уже закрыта.")
        notify_ids = await close_in_session(session, group)
        service = await session.get(Service, group.service_id)
        return Closure(group, service, notify_ids)  # type: ignore[arg-type]


async def list_user_groups(session_maker: SessionMaker, user_id: int) -> list[GroupEntry]:
    """Группы, где пользователь организатор или участник (в том числе с поданной заявкой)."""
    async with session_maker() as session:
        owned = await session.execute(
            select(Group, Service)
            .join(Service, Group.service_id == Service.id)
            .where(Group.owner_id == user_id, Group.status != GroupStatus.CLOSED)
            .order_by(Group.id.desc())
        )
        joined = await session.execute(
            select(Group, Service, Membership.status)
            .join(Service, Group.service_id == Service.id)
            .join(Membership, Membership.group_id == Group.id)
            .where(
                Membership.user_id == user_id,
                Membership.status.in_(ACTIVE),
                Group.status != GroupStatus.CLOSED,
            )
            .order_by(Group.id.desc())
        )
        entries = [GroupEntry(g, s, "owner", None) for g, s in owned]
        entries += [GroupEntry(g, s, "member", st) for g, s, st in joined]
        return entries


async def get_group_view(
    session_maker: SessionMaker, group_id: int, user_id: int
) -> GroupView | None:
    async with session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.status == GroupStatus.CLOSED:
            return None
        service = await session.get(Service, group.service_id)
        if group.owner_id == user_id:
            return GroupView(group, service, "owner", None, None)  # type: ignore[arg-type]
        membership = await session.scalar(
            select(Membership).where(
                Membership.group_id == group_id,
                Membership.user_id == user_id,
                Membership.status.in_(ACTIVE),
            )
        )
        if membership is None:
            return None
        contact = None
        if membership.status == MembershipStatus.APPROVED:
            contact = await session.get(User, group.owner_id)
        return GroupView(group, service, "member", membership.status, contact)  # type: ignore[arg-type]


async def list_members(session_maker: SessionMaker, group_id: int, owner_id: int) -> list[User]:
    async with session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.owner_id != owner_id:
            raise GroupError("Это не ваша группа.")
        rows = await session.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.group_id == group_id, Membership.status == MembershipStatus.APPROVED)
            .order_by(Membership.joined_at)
        )
        return list(rows)
