from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Group,
    GroupStatus,
    Membership,
    MembershipStatus,
    Report,
    ReportStatus,
    Service,
    User,
)
from shared.services.groups import GroupError, SessionMaker, lock_group
from shared.services.memberships import ACTIVE, close_in_session, release_membership

REASONS = {
    "unpaid": "Не платит",
    "gone": "Пропал",
    "fraud": "Мошенничество",
    "other": "Другое",
}
MAX_COMMENT = 500
OPEN_REPORTS_LIMIT = 10


class AlreadyBannedError(GroupError):
    pass


@dataclass
class ClosedGroup:
    title: str
    notify_ids: list[int]


@dataclass
class LeftGroup:
    owner_telegram_id: int
    title: str


@dataclass
class BanResult:
    user: User
    closed: list[ClosedGroup]
    left: list[LeftGroup]


@dataclass
class ReportView:
    report: Report
    reporter: User
    target: User
    service: Service | None


@dataclass
class ReportOutcome:
    report: Report
    ban: BanResult | None


def normalize_username(raw: str) -> str:
    return raw.strip().removeprefix("@").lower()


async def find_user_by_username(session_maker: SessionMaker, raw: str) -> User:
    username = normalize_username(raw)
    if not username:
        raise GroupError("Укажите username, например /ban @name.")
    async with session_maker() as session:
        user = await session.scalar(
            select(User).where(func.lower(User.username) == username).order_by(User.id.desc())
        )
    if user is None:
        raise GroupError(f"Пользователь @{username} не найден: он не запускал бота.")
    return user


async def is_banned(session_maker: SessionMaker, telegram_id: int) -> bool:
    async with session_maker() as session:
        banned = await session.scalar(select(User.is_banned).where(User.telegram_id == telegram_id))
    return bool(banned)


async def ban_user(session_maker: SessionMaker, user_id: int, protected_ids: set[int]) -> BanResult:
    """Банит пользователя, закрывает его группы и убирает из чужих групп."""
    async with session_maker() as session, session.begin():
        user = await session.get(User, user_id)
        if user is None:
            raise GroupError("Пользователь не найден.")
        if user.telegram_id in protected_ids:
            raise GroupError("Администратора забанить нельзя.")
        if user.is_banned:
            raise AlreadyBannedError("Пользователь уже забанен.")
        # Блокируем все затрагиваемые группы по возрастанию id: единый порядок защищает
        # от deadlock с параллельными принятиями, выходами и другими банами
        for group_id in await _affected_group_ids(session, user.id):
            await lock_group(session, group_id)
        user.is_banned = True
        closed = await _close_owned_groups(session, user.id)
        left = await _leave_joined_groups(session, user.id)
        return BanResult(user, closed, left)


async def _affected_group_ids(session: AsyncSession, user_id: int) -> list[int]:
    owned = await session.scalars(
        select(Group.id).where(Group.owner_id == user_id, Group.status != GroupStatus.CLOSED)
    )
    joined = await session.scalars(
        select(Membership.group_id).where(
            Membership.user_id == user_id, Membership.status.in_(ACTIVE)
        )
    )
    return sorted({*owned, *joined})


async def _close_owned_groups(session: AsyncSession, user_id: int) -> list[ClosedGroup]:
    rows = await session.execute(
        select(Group, Service)
        .join(Service, Group.service_id == Service.id)
        .where(Group.owner_id == user_id, Group.status != GroupStatus.CLOSED)
    )
    closed = []
    for group, service in rows.all():
        notify_ids = await close_in_session(session, group)
        closed.append(ClosedGroup(f"{service.name} {service.plan_name}", notify_ids))
    return closed


async def _leave_joined_groups(session: AsyncSession, user_id: int) -> list[LeftGroup]:
    memberships = await session.scalars(
        select(Membership).where(Membership.user_id == user_id, Membership.status.in_(ACTIVE))
    )
    left = []
    for membership in list(memberships):
        group = await release_membership(session, membership)
        service = await session.get(Service, group.service_id)
        owner = await session.get(User, group.owner_id)
        left.append(LeftGroup(owner.telegram_id, f"{service.name} {service.plan_name}"))  # type: ignore[union-attr]
    return left


async def unban_user(session_maker: SessionMaker, user_id: int) -> User:
    async with session_maker() as session, session.begin():
        user = await session.get(User, user_id)
        if user is None:
            raise GroupError("Пользователь не найден.")
        if not user.is_banned:
            raise GroupError("Пользователь не забанен.")
        user.is_banned = False
        return user


async def _participant_ids(session: AsyncSession, group: Group) -> set[int]:
    rows = await session.scalars(
        select(Membership.user_id).where(
            Membership.group_id == group.id, Membership.status == MembershipStatus.APPROVED
        )
    )
    return {group.owner_id, *rows}


async def _was_removed_by(
    session: AsyncSession, group_id: int, user_id: int, remover_id: int
) -> bool:
    """Удалённый участник вправе пожаловаться на организатора, который его удалил."""
    found = await session.scalar(
        select(Membership.id).where(
            Membership.group_id == group_id,
            Membership.user_id == user_id,
            Membership.status == MembershipStatus.REMOVED,
            Membership.removed_by == remover_id,
        )
    )
    return found is not None


async def create_report(
    session_maker: SessionMaker,
    reporter_id: int,
    group_id: int,
    target_id: int,
    reason_key: str,
    comment: str,
) -> Report:
    """Жалоба на участника или организатора группы, в которой состоят оба."""
    comment = comment.strip()
    if reason_key not in REASONS:
        raise GroupError("Выберите причину из списка.")
    if reason_key == "other" and not comment:
        raise GroupError("Для причины «Другое» опишите проблему.")
    if len(comment) > MAX_COMMENT:
        raise GroupError(f"Комментарий слишком длинный, максимум {MAX_COMMENT} символов.")
    if reporter_id == target_id:
        raise GroupError("Нельзя пожаловаться на себя.")
    async with session_maker() as session, session.begin():
        group = await session.get(Group, group_id)
        if group is None:
            raise GroupError("Группа не найдена.")
        participants = await _participant_ids(session, group)
        removed_by_target = await _was_removed_by(session, group_id, reporter_id, target_id)
        if target_id not in participants or (
            reporter_id not in participants and not removed_by_target
        ):
            raise GroupError("Пожаловаться можно только на участника своей группы.")
        duplicate = await session.scalar(
            select(Report.id).where(
                Report.reporter_id == reporter_id,
                Report.target_user_id == target_id,
                Report.group_id == group_id,
                Report.status == ReportStatus.NEW,
            )
        )
        if duplicate is not None:
            raise GroupError("Вы уже отправляли жалобу на этого пользователя, она на рассмотрении.")
        label = REASONS[reason_key]
        report = Report(
            reporter_id=reporter_id,
            target_user_id=target_id,
            group_id=group_id,
            reason=f"{label}: {comment}" if comment else label,
        )
        session.add(report)
        await session.flush()
        return report


async def list_open_reports(session_maker: SessionMaker) -> list[ReportView]:
    async with session_maker() as session:
        reports = await session.scalars(
            select(Report)
            .where(Report.status == ReportStatus.NEW)
            .order_by(Report.id)
            .limit(OPEN_REPORTS_LIMIT)
        )
        views = []
        for report in reports:
            group = await session.get(Group, report.group_id) if report.group_id else None
            service = await session.get(Service, group.service_id) if group else None
            views.append(
                ReportView(
                    report,
                    await session.get(User, report.reporter_id),  # type: ignore[arg-type]
                    await session.get(User, report.target_user_id),  # type: ignore[arg-type]
                    service,
                )
            )
        return views


async def resolve_report(
    session_maker: SessionMaker, report_id: int, ban: bool, protected_ids: set[int]
) -> ReportOutcome:
    """Закрывает жалобу: с баном нарушителя или отклонением."""
    async with session_maker() as session:
        report = await session.get(Report, report_id)
    if report is None:
        raise GroupError("Жалоба не найдена.")
    if report.status != ReportStatus.NEW:
        raise GroupError("Жалоба уже рассмотрена.")
    ban_result = None
    if ban:
        try:
            ban_result = await ban_user(session_maker, report.target_user_id, protected_ids)
        except AlreadyBannedError:
            pass  # цель уже забанена, жалобу всё равно нужно закрыть
    async with session_maker() as session, session.begin():
        result = await session.execute(
            update(Report)
            .where(Report.id == report_id, Report.status == ReportStatus.NEW)
            .values(status=ReportStatus.RESOLVED if ban else ReportStatus.DISMISSED)
        )
        if result.rowcount == 0:
            raise GroupError("Жалоба уже рассмотрена.")
        report = await session.get(Report, report_id)  # type: ignore[assignment]
        await session.refresh(report)
    return ReportOutcome(report, ban_result)
