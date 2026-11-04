from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from bot.services.groups import GroupError, Role, SessionMaker, role_in_session

MAX_SHOWN_APPLICANTS = 10
ACTIVE = (MembershipStatus.PENDING, MembershipStatus.APPROVED)


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
class Applicant:
    membership_id: int
    user: User


@dataclass
class GroupCard:
    """Данные карточки группы. Что попало в карточку, определено ролью смотрящего."""

    group: Group
    service: Service
    role: Role
    owner: User | None  # контакт организатора: только участнику с одобренной заявкой
    participants: list[User]  # только организатору и участникам
    applicants: list[Applicant]  # ждущие заявки: только организатору


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


async def get_group_card(
    session_maker: SessionMaker, group_id: int, viewer_id: int
) -> GroupCard | None:
    """Карточка группы для конкретного пользователя; чужие данные отдаёт только по роли."""
    async with session_maker() as session:
        group = await session.get(Group, group_id)
        if group is None or group.status == GroupStatus.CLOSED:
            return None
        role = await role_in_session(session, group, viewer_id)
        if role == Role.NONE and (group.status != GroupStatus.OPEN or group.slots_free < 1):
            return None  # посторонний видит только открытые группы со свободными местами
        service = await session.get(Service, group.service_id)
        participants: list[User] = []
        applicants: list[Applicant] = []
        owner = None
        if role in (Role.OWNER, Role.MEMBER):
            rows = await session.scalars(
                select(User)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.group_id == group_id,
                    Membership.status == MembershipStatus.APPROVED,
                )
                .order_by(Membership.joined_at, Membership.id)
            )
            participants = list(rows)
        if role == Role.MEMBER:
            owner = await session.get(User, group.owner_id)
        if role == Role.OWNER:
            pending = await session.execute(
                select(Membership.id, User)
                .join(User, Membership.user_id == User.id)
                .where(
                    Membership.group_id == group_id,
                    Membership.status == MembershipStatus.PENDING,
                )
                .order_by(Membership.id)
                .limit(MAX_SHOWN_APPLICANTS)
            )
            applicants = [Applicant(mid, user) for mid, user in pending]
        return GroupCard(group, service, role, owner, participants, applicants)  # type: ignore[arg-type]


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
