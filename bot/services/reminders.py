import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select, update

from shared.dates import ALMATY, next_payment_date
from shared.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from shared.notifications import NotificationType, enqueue, user_ref
from shared.services.groups import SessionMaker

logger = logging.getLogger(__name__)

REMINDER_HOUR = 10


class ReminderKind(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"


def reminder_kind(billing_day: int, today: date) -> ReminderKind | None:
    days_left = (next_payment_date(billing_day, today) - today).days
    return {0: ReminderKind.TODAY, 1: ReminderKind.TOMORROW}.get(days_left)


@dataclass
class _Due:
    group: Group
    service: Service
    owner: User
    members: list[User]
    kind: ReminderKind


async def _find_due(session_maker: SessionMaker, today: date) -> list[_Due]:
    due: list[_Due] = []
    async with session_maker() as session:
        rows = await session.execute(
            select(Group, Service, User)
            .join(Service, Group.service_id == Service.id)
            .join(User, Group.owner_id == User.id)
            .where(Group.status != GroupStatus.CLOSED)
        )
        for group, service, owner in rows.all():
            kind = reminder_kind(group.billing_day, today)
            if kind is None:
                continue
            members = await session.scalars(
                select(User)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.group_id == group.id,
                    Membership.status == MembershipStatus.APPROVED,
                )
            )
            due.append(_Due(group, service, owner, list(members), kind))
    return due


async def _enqueue_group(session_maker: SessionMaker, d: _Due, today: date) -> int:
    """В одной транзакции помечает группу напомненной и кладёт уведомления в очередь."""
    async with session_maker() as session, session.begin():
        claimed = await session.execute(
            update(Group)
            .where(
                Group.id == d.group.id,
                or_(Group.last_reminded_on.is_(None), Group.last_reminded_on < today),
            )
            .values(last_reminded_on=today)
        )
        if claimed.rowcount != 1:
            return 0  # уже напомнили сегодня (повторный запуск)
        base = {
            "group_id": d.group.id,
            "title": f"{d.service.name} {d.service.plan_name}",
            "when": d.kind.value,
            "price": str(d.group.price_per_member),
            "currency": d.service.currency,
        }
        owner = {**base, "role": "owner", "members": len(d.members)}
        await enqueue(session, d.owner.id, NotificationType.PAYMENT_REMINDER, owner)
        member = {**base, "role": "member", **user_ref(d.owner, "owner")}
        for user in d.members:
            await enqueue(session, user.id, NotificationType.PAYMENT_REMINDER, member)
    return 1 + len(d.members)


async def enqueue_daily_reminders(session_maker: SessionMaker, today: date | None = None) -> int:
    """Ставит в очередь напоминания о сегодняшней и завтрашней оплате. Возвращает число событий.

    Отправляет их воркер очереди (bot/outbox.py), поэтому сбой Telegram не теряет напоминания.
    """
    today = today or datetime.now(ALMATY).date()
    queued = 0
    for d in await _find_due(session_maker, today):
        if not d.members:
            continue  # собирать взносы не с кого
        try:
            queued += await _enqueue_group(session_maker, d, today)
        except Exception:
            logger.exception("Сбой напоминания по группе %s", d.group.id)
    logger.info("Напоминания за %s: в очередь поставлено %s", today, queued)
    return queued


def start_reminders(session_maker: SessionMaker) -> AsyncIOScheduler:
    """Ежедневное задание в 10:00 по Алматы. Вызывать из работающего event loop."""
    scheduler = AsyncIOScheduler(timezone=ALMATY)
    scheduler.add_job(
        enqueue_daily_reminders,
        CronTrigger(hour=REMINDER_HOUR, minute=0, timezone=ALMATY),
        args=(session_maker,),
        id="daily_reminders",
        coalesce=True,
        misfire_grace_time=3600,
    )
    if datetime.now(ALMATY).hour >= REMINDER_HOUR:
        # Задания хранятся в памяти: если бот перезапустили после 10:00, догоняем сегодняшний
        # запуск. Дубли исключены пометкой last_reminded_on.
        scheduler.add_job(enqueue_daily_reminders, args=(session_maker,))
    scheduler.start()
    return scheduler
