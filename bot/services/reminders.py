import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select, update

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from bot.services.groups import SessionMaker
from bot.texts import contact_html, money
from bot.ui import notify

logger = logging.getLogger(__name__)

# Алматы: UTC+5 круглый год
ALMATY = timezone(timedelta(hours=5))
REMINDER_HOUR = 10


class ReminderKind(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"


def effective_billing_day(billing_day: int, year: int, month: int) -> int:
    """Если в месяце меньше дней, чем billing_day, оплата в последний день месяца."""
    return min(billing_day, calendar.monthrange(year, month)[1])


def next_payment_date(billing_day: int, today: date) -> date:
    """Ближайшая дата оплаты, не раньше сегодняшнего дня."""
    this_month = date(
        today.year, today.month, effective_billing_day(billing_day, today.year, today.month)
    )
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, effective_billing_day(billing_day, year, month))


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


async def _claim_group(session_maker: SessionMaker, group_id: int, today: date) -> bool:
    """Помечает группу как напомненную сегодня; False, если уже помечена (повторный запуск)."""
    async with session_maker() as session, session.begin():
        result = await session.execute(
            update(Group)
            .where(
                Group.id == group_id,
                or_(Group.last_reminded_on.is_(None), Group.last_reminded_on < today),
            )
            .values(last_reminded_on=today)
        )
        return result.rowcount == 1


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


def _when(kind: ReminderKind) -> str:
    return "Сегодня" if kind == ReminderKind.TODAY else "Завтра"


def member_text(d: _Due) -> str:
    owner = contact_html(d.owner.telegram_id, d.owner.username)
    return (
        f"{_when(d.kind)} оплата подписки {d.service.name} {d.service.plan_name}.\n"
        f"Ваш взнос: {money(d.group.price_per_member, d.service.currency)}.\n"
        f"Переведите его организатору: {owner}."
    )


def owner_text(d: _Due) -> str:
    return (
        f"{_when(d.kind)} оплата подписки {d.service.name} {d.service.plan_name}.\n"
        f"Участники (их {len(d.members)}) переведут вам по "
        f"{money(d.group.price_per_member, d.service.currency)}."
    )


async def send_daily_reminders(
    bot: Bot, session_maker: SessionMaker, today: date | None = None
) -> int:
    """Рассылает напоминания о сегодняшней и завтрашней оплате. Возвращает число отправленных."""
    today = today or datetime.now(ALMATY).date()
    sent = 0
    for d in await _find_due(session_maker, today):
        if not d.members:
            continue  # собирать взносы не с кого
        try:
            if not await _claim_group(session_maker, d.group.id, today):
                continue
            sent += await _send_group(bot, d)
        except Exception:
            logger.exception("Сбой напоминания по группе %s", d.group.id)
    logger.info("Напоминания за %s: отправлено %s", today, sent)
    return sent


async def _send_group(bot: Bot, d: _Due) -> int:
    # notify сам гасит ошибки отправки (например, бот заблокирован) и пишет их в лог
    sent = int(await notify(bot, d.owner.telegram_id, owner_text(d), html=True))
    text = member_text(d)
    for member in d.members:
        sent += int(await notify(bot, member.telegram_id, text, html=True))
    return sent


def start_reminders(bot: Bot, session_maker: SessionMaker) -> AsyncIOScheduler:
    """Ежедневное задание в 10:00 по Алматы. Вызывать из работающего event loop."""
    scheduler = AsyncIOScheduler(timezone=ALMATY)
    scheduler.add_job(
        send_daily_reminders,
        CronTrigger(hour=REMINDER_HOUR, minute=0, timezone=ALMATY),
        args=(bot, session_maker),
        id="daily_reminders",
        coalesce=True,
        misfire_grace_time=3600,
    )
    if datetime.now(ALMATY).hour >= REMINDER_HOUR:
        # Задания хранятся в памяти: если бот перезапустили после 10:00, догоняем сегодняшний
        # запуск. Дубли исключены пометкой last_reminded_on.
        scheduler.add_job(send_daily_reminders, args=(bot, session_maker))
    scheduler.start()
    return scheduler
