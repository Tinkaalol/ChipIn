from datetime import date
from decimal import Decimal

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select

from bot.outbox import process_outbox
from bot.services import reminders as r
from shared.dates import effective_billing_day
from shared.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User


@pytest.mark.parametrize(
    ("billing_day", "year", "month", "expected"),
    [
        (15, 2026, 6, 15),
        (28, 2026, 2, 28),
        (31, 2026, 2, 28),  # не високосный февраль
        (31, 2028, 2, 29),  # високосный
        (30, 2028, 2, 29),
        (31, 2026, 4, 30),
        (31, 2026, 1, 31),
        (29, 2026, 2, 28),
    ],
)
def test_effective_billing_day(billing_day: int, year: int, month: int, expected: int) -> None:
    assert effective_billing_day(billing_day, year, month) == expected


@pytest.mark.parametrize(
    ("billing_day", "today", "expected"),
    [
        (10, date(2026, 6, 3), date(2026, 6, 10)),  # впереди в этом месяце
        (10, date(2026, 6, 10), date(2026, 6, 10)),  # сегодня
        (10, date(2026, 6, 11), date(2026, 7, 10)),  # уже прошло: следующий месяц
        (10, date(2026, 12, 20), date(2027, 1, 10)),  # переход через год
        (31, date(2026, 2, 1), date(2026, 2, 28)),  # короткий месяц
        (31, date(2028, 2, 1), date(2028, 2, 29)),
        (31, date(2026, 4, 30), date(2026, 4, 30)),  # апрель: 31-е = 30-е
        (31, date(2026, 5, 1), date(2026, 5, 31)),
        (31, date(2026, 2, 28), date(2026, 2, 28)),
        (31, date(2026, 3, 1), date(2026, 3, 31)),
        (30, date(2026, 12, 31), date(2027, 1, 30)),
        (1, date(2026, 1, 1), date(2026, 1, 1)),
    ],
)
def test_next_payment_date(billing_day: int, today: date, expected: date) -> None:
    assert r.next_payment_date(billing_day, today) == expected


@pytest.mark.parametrize(
    ("billing_day", "today", "expected"),
    [
        (10, date(2026, 6, 9), r.ReminderKind.TOMORROW),
        (10, date(2026, 6, 10), r.ReminderKind.TODAY),
        (10, date(2026, 6, 8), None),
        (10, date(2026, 6, 11), None),
        (1, date(2026, 5, 31), r.ReminderKind.TOMORROW),  # граница месяцев
        (1, date(2026, 12, 31), r.ReminderKind.TOMORROW),  # граница года
        (31, date(2026, 2, 27), r.ReminderKind.TOMORROW),  # оплата 31-го = 28 февраля
        (31, date(2026, 2, 28), r.ReminderKind.TODAY),
        (31, date(2026, 3, 30), r.ReminderKind.TOMORROW),
    ],
)
def test_reminder_kind(billing_day: int, today: date, expected: r.ReminderKind | None) -> None:
    assert r.reminder_kind(billing_day, today) == expected


class FakeBot:
    def __init__(self, blocked: set[int] | None = None) -> None:
        self.blocked = blocked or set()
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if chat_id in self.blocked:
            method = SendMessage(chat_id=chat_id, text=text)
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        self.sent.append((chat_id, text))


async def run(bot: FakeBot, sm, today: date) -> int:
    """Ставит напоминания в очередь и сразу отправляет их; возвращает число событий."""
    queued = await r.enqueue_daily_reminders(sm, today)
    await process_outbox(bot, sm)  # type: ignore[arg-type]
    return queued


async def make_group(
    sm, billing_day: int = 10, status: GroupStatus = GroupStatus.OPEN, members: int = 2
):
    async with sm() as s:
        owner = User(telegram_id=1, username="owner")
        service = Service(
            name="Spotify", plan_name="Family", price_month=Decimal("300"), max_members=6
        )
        users = [User(telegram_id=10 + i, username=f"m{i}") for i in range(members)]
        s.add_all([owner, service, *users])
        await s.flush()
        group = Group(
            service_id=service.id,
            owner_id=owner.id,
            price_per_member=Decimal("100"),
            slots_total=4,
            slots_free=4 - 1 - members,
            billing_day=billing_day,
            status=status,
        )
        s.add(group)
        await s.flush()
        for u in users:
            s.add(Membership(group_id=group.id, user_id=u.id, status=MembershipStatus.APPROVED))
        await s.commit()
        return group


async def test_reminds_owner_and_approved_members_with_amount(session_maker) -> None:
    await make_group(session_maker, billing_day=10)
    bot = FakeBot()
    sent = await run(bot, session_maker, date(2026, 6, 9))
    assert sent == 3
    assert {chat for chat, _ in bot.sent} == {1, 10, 11}
    assert all(text.startswith("Завтра") for _, text in bot.sent)
    member_text = next(t for c, t in bot.sent if c == 10)
    assert "100.00 ₸" in member_text and "@owner" in member_text


async def test_today_reminder_text(session_maker) -> None:
    await make_group(session_maker, billing_day=10)
    bot = FakeBot()
    await run(bot, session_maker, date(2026, 6, 10))
    assert all(text.startswith("Сегодня") for _, text in bot.sent)


async def test_no_reminder_on_other_days(session_maker) -> None:
    await make_group(session_maker, billing_day=10)
    bot = FakeBot()
    assert await run(bot, session_maker, date(2026, 6, 5)) == 0
    assert bot.sent == []


async def test_not_sent_twice_in_one_day(session_maker) -> None:
    await make_group(session_maker, billing_day=10)
    bot = FakeBot()
    today = date(2026, 6, 9)
    assert await run(bot, session_maker, today) == 3
    assert await run(bot, session_maker, today) == 0
    assert len(bot.sent) == 3
    # на следующий день приходит второе напоминание («сегодня»)
    assert await run(bot, session_maker, date(2026, 6, 10)) == 3


async def test_blocked_user_does_not_stop_the_rest(session_maker) -> None:
    await make_group(session_maker, billing_day=10)
    bot = FakeBot(blocked={10})
    assert await run(bot, session_maker, date(2026, 6, 9)) == 3
    assert {chat for chat, _ in bot.sent} == {1, 11}  # заблокировавшему не доставлено


async def test_skips_pending_left_closed_and_empty_groups(session_maker) -> None:
    group = await make_group(session_maker, billing_day=10, members=2)
    async with session_maker() as s:
        memberships = (await s.scalars(select(Membership))).all()
        memberships[0].status = MembershipStatus.LEFT
        memberships[1].status = MembershipStatus.PENDING
        await s.commit()
    bot = FakeBot()
    # одобренных участников нет — напоминать не о чем
    assert await run(bot, session_maker, date(2026, 6, 9)) == 0

    async with session_maker() as s:
        (await s.get(Membership, memberships[0].id)).status = MembershipStatus.APPROVED
        (await s.get(Group, group.id)).status = GroupStatus.CLOSED
        await s.commit()
    assert await run(bot, session_maker, date(2026, 6, 9)) == 0


async def test_full_group_is_reminded(session_maker) -> None:
    await make_group(session_maker, billing_day=10, status=GroupStatus.FULL)
    bot = FakeBot()
    assert await run(bot, session_maker, date(2026, 6, 9)) == 3


async def test_scheduler_runs_daily_at_ten_almaty(session_maker) -> None:
    scheduler = r.start_reminders(session_maker)
    try:
        job = scheduler.get_job("daily_reminders")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "10" and fields["minute"] == "0"
        assert job.trigger.timezone.utcoffset(None).total_seconds() == 5 * 3600
    finally:
        scheduler.shutdown(wait=False)
