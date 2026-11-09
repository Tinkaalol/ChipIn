import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import func, select, update

from bot import outbox as worker
from bot.handlers import my_groups
from shared import notifications as n
from shared.db.models import (
    Membership,
    NotificationOutbox,
    NotificationStatus,
    User,
)
from shared.services import groups as groups_svc
from shared.services import memberships as ms
from shared.services import moderation as mod
from tests.fakes import outbox
from tests.test_memberships import make_world

T = n.NotificationType


async def count_events(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(NotificationOutbox))


# --- запись вместе с транзакцией ---


async def test_event_is_saved_with_the_transaction(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker)
    await groups_svc.request_join(session_maker, group.id, u1.id)
    events = await outbox(session_maker)
    assert [(e.type, e.user_id, e.status) for e in events] == [
        (T.NEW_JOIN_REQUEST, owner.id, NotificationStatus.PENDING)
    ]
    assert events[0].payload["group_id"] == group.id and events[0].attempts == 0


async def test_event_disappears_when_transaction_rolls_back(session_maker) -> None:
    owner, *_ = await make_world(session_maker)
    async with session_maker() as session:
        await n.enqueue(session, owner.id, T.ACCOUNT_BLOCKED, {})
        await session.rollback()
    assert await count_events(session_maker) == 0


async def test_failure_after_enqueue_leaves_neither_event_nor_membership(
    session_maker, monkeypatch
) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker)
    real = groups_svc.enqueue

    async def enqueue_then_fail(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("сбой после постановки в очередь")

    monkeypatch.setattr(groups_svc, "enqueue", enqueue_then_fail)
    with pytest.raises(RuntimeError):
        await groups_svc.request_join(session_maker, group.id, u1.id)
    async with session_maker() as s:
        assert await s.scalar(select(func.count()).select_from(Membership)) == 0
    assert await count_events(session_maker) == 0


async def test_failed_business_operation_queues_nothing(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=1)
    first = await groups_svc.request_join(session_maker, group.id, u1.id)
    second = await groups_svc.request_join(session_maker, group.id, u2.id)
    await groups_svc.approve_member(session_maker, first.membership.id, owner.id)
    before = await count_events(session_maker)
    with pytest.raises(groups_svc.GroupError):  # мест нет
        await groups_svc.approve_member(session_maker, second.membership.id, owner.id)
    assert await count_events(session_maker) == before


# --- skipped ---


async def test_unlinked_user_gets_skipped(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker)
    async with session_maker() as s:
        (await s.get(User, owner.id)).telegram_id = None  # Telegram отвязан
        await s.commit()
    await groups_svc.request_join(session_maker, group.id, u1.id)
    (event,) = await outbox(session_maker)
    assert event.status == NotificationStatus.SKIPPED and "не привязан" in event.error


async def test_disabled_notifications_get_skipped(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker)
    async with session_maker() as s:
        (await s.get(User, owner.id)).notify_enabled = False
        await s.commit()
    await groups_svc.request_join(session_maker, group.id, u1.id)
    (event,) = await outbox(session_maker)
    assert event.status == NotificationStatus.SKIPPED and "выключены" in event.error


# --- какие события создаёт бизнес-логика ---


async def test_events_for_the_whole_group_lifecycle(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    a2 = await groups_svc.request_join(session_maker, group.id, u2.id)
    await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    await groups_svc.reject_member(session_maker, a2.membership.id, owner.id)
    await ms.leave_group(session_maker, group.id, u1.id)
    a3 = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, a3.membership.id, owner.id)
    await ms.remove_member(session_maker, a3.membership.id, owner.id)
    await ms.set_removal_reason(session_maker, a3.membership.id, owner.id, "Не платил")
    got = [(e.type, e.user_id) for e in await outbox(session_maker)]
    assert got == [
        (T.NEW_JOIN_REQUEST, owner.id),
        (T.NEW_JOIN_REQUEST, owner.id),
        (T.REQUEST_APPROVED, u1.id),
        (T.REQUEST_REJECTED, u2.id),
        (T.MEMBER_LEFT, owner.id),
        (T.NEW_JOIN_REQUEST, owner.id),
        (T.REQUEST_APPROVED, u1.id),
        (T.MEMBER_REMOVED, u1.id),
        (T.MEMBER_REMOVED, u1.id),
    ]
    reasons = [e.payload["reason"] for e in await outbox(session_maker, T.MEMBER_REMOVED)]
    assert reasons == [None, "Не платил"]


async def test_ban_events(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    await groups_svc.request_join(session_maker, group.id, u2.id)
    await mod.ban_user(session_maker, u1.id, protected_ids=set())  # участник
    assert [e.user_id for e in await outbox(session_maker, T.SLOT_FREED)] == [owner.id]
    assert [e.user_id for e in await outbox(session_maker, T.ACCOUNT_BLOCKED)] == [u1.id]
    await mod.ban_user(session_maker, owner.id, protected_ids=set())  # организатор
    closed = await outbox(session_maker, T.GROUP_CLOSED)
    assert [e.user_id for e in closed] == [u2.id]
    await mod.unban_user(session_maker, owner.id)
    assert [e.user_id for e in await outbox(session_maker, T.ACCOUNT_UNBLOCKED)] == [owner.id]


# --- воркер ---


class Bot:
    def __init__(self, error: Exception | None = None, blocked: set[int] | None = None) -> None:
        self.error, self.blocked = error, blocked or set()
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        if self.error:
            raise self.error
        if chat_id in self.blocked:
            raise TelegramForbiddenError(SendMessage(chat_id=chat_id, text=text), "blocked")
        self.sent.append((chat_id, text, reply_markup))


async def one_event(sm):
    owner, _, group, (u1, _) = await make_world(sm)
    await groups_svc.request_join(sm, group.id, u1.id)
    return owner


async def only(sm) -> NotificationOutbox:
    (event,) = await outbox(sm)
    return event


async def test_worker_sends_and_marks_sent(session_maker) -> None:
    owner = await one_event(session_maker)
    bot = Bot()
    stats = await worker.process_outbox(bot, session_maker, "https://site.example")  # type: ignore[arg-type]
    assert stats["sent"] == 1
    chat, text, markup = bot.sent[0]
    assert chat == owner.telegram_id and "Новая заявка" in text
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert urls == [
        f"https://site.example/groups/{(await only(session_maker)).payload['group_id']}"
    ]
    event = await only(session_maker)
    assert event.status == NotificationStatus.SENT and event.sent_at is not None
    assert event.attempts == 1
    # повторный проход ничего не шлёт
    assert (await worker.process_outbox(bot, session_maker))["sent"] == 0  # type: ignore[arg-type]
    assert len(bot.sent) == 1


async def test_network_error_is_retried_with_backoff_then_fails(session_maker) -> None:
    await one_event(session_maker)
    bot = Bot(error=TelegramNetworkError(SendMessage(chat_id=1, text="x"), "boom"))
    assert (await worker.process_outbox(bot, session_maker))["retry"] == 1  # type: ignore[arg-type]
    event = await only(session_maker)
    assert event.status == NotificationStatus.PENDING and event.attempts == 1
    wait = event.next_attempt_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(seconds=20) < wait <= worker.BASE_BACKOFF
    # до срока событие не трогают
    assert not any((await worker.process_outbox(bot, session_maker)).values())  # type: ignore[arg-type]
    # доводим до предела попыток
    for _ in range(worker.MAX_ATTEMPTS):
        async with session_maker() as s:
            await s.execute(update(NotificationOutbox).values(next_attempt_at=datetime.now(UTC)))
            await s.commit()
        await worker.process_outbox(bot, session_maker)  # type: ignore[arg-type]
    event = await only(session_maker)
    assert event.status == NotificationStatus.FAILED and event.attempts == worker.MAX_ATTEMPTS
    assert "boom" in event.error


async def test_blocked_bot_fails_immediately_without_retries(session_maker) -> None:
    owner = await one_event(session_maker)
    bot = Bot(blocked={owner.telegram_id})
    assert (await worker.process_outbox(bot, session_maker))["failed"] == 1  # type: ignore[arg-type]
    event = await only(session_maker)
    assert event.status == NotificationStatus.FAILED and event.attempts == 1


async def test_recipient_unlinked_after_enqueue_is_skipped_at_send_time(session_maker) -> None:
    owner = await one_event(session_maker)
    async with session_maker() as s:
        (await s.get(User, owner.id)).telegram_id = None
        await s.commit()
    bot = Bot()
    assert (await worker.process_outbox(bot, session_maker))["skipped"] == 1  # type: ignore[arg-type]
    assert bot.sent == [] and (await only(session_maker)).status == NotificationStatus.SKIPPED


async def test_concurrent_workers_send_once(file_session_maker) -> None:
    await one_event(file_session_maker)
    bot = Bot()
    await asyncio.gather(
        *(worker.process_outbox(bot, file_session_maker) for _ in range(4))  # type: ignore[arg-type]
    )
    assert len(bot.sent) == 1


async def test_stuck_event_is_retried_after_lease(session_maker) -> None:
    await one_event(session_maker)
    assert await worker._claim(session_maker, (await only(session_maker)).id)
    bot = Bot()
    # воркер «упал» после claim: пока аренда не истекла, событие не берут
    assert not any((await worker.process_outbox(bot, session_maker)).values())  # type: ignore[arg-type]
    async with session_maker() as s:
        await s.execute(update(NotificationOutbox).values(next_attempt_at=datetime.now(UTC)))
        await s.commit()
    assert (await worker.process_outbox(bot, session_maker))["sent"] == 1  # type: ignore[arg-type]


# --- шаблоны ---

SAMPLES: dict[n.NotificationType, dict] = {
    T.NEW_JOIN_REQUEST: {
        "group_id": 5,
        "membership_id": 9,
        "title": "Spotify Family",
        "slots_free": 2,
        "applicant_username": "bob",
        "applicant_telegram_id": 2,
    },
    T.REQUEST_APPROVED: {
        "group_id": 5,
        "title": "Spotify Family",
        "billing_day": 7,
        "price": "933.34",
        "currency": "KZT",
        "owner_username": "alice",
        "owner_telegram_id": 1,
    },
    T.REQUEST_REJECTED: {"group_id": 5, "title": "Spotify Family"},
    T.MEMBER_LEFT: {
        "group_id": 5,
        "title": "Spotify Family",
        "was_approved": True,
        "member_username": "bob",
        "member_telegram_id": 2,
    },
    T.MEMBER_REMOVED: {"group_id": 5, "title": "Spotify Family", "owner_id": 1, "reason": None},
    T.SLOT_FREED: {
        "group_id": 5,
        "title": "Spotify Family",
        "member_username": "bob",
        "member_telegram_id": 2,
    },
    T.PAYMENT_REMINDER: {
        "group_id": 5,
        "title": "Spotify Family",
        "when": "today",
        "price": "100",
        "currency": "KZT",
        "role": "member",
        "owner_username": "alice",
        "owner_telegram_id": 1,
    },
    T.ACCOUNT_BLOCKED: {},
    T.ACCOUNT_UNBLOCKED: {},
    T.GROUP_CLOSED: {"group_id": 5, "title": "Spotify Family", "by_ban": False},
}


def test_every_type_has_a_russian_template_and_site_links() -> None:
    assert set(SAMPLES) == set(n.NotificationType) == set(n._TEMPLATES)
    for kind, payload in SAMPLES.items():
        plain = n.render(kind, payload, "")
        linked = n.render(kind, payload, "https://site.example/", today=date(2026, 6, 1))
        assert plain.text and any("а" <= ch.lower() <= "я" for ch in plain.text), kind
        urls = [b.url for row in linked.buttons for b in row if b.url]
        assert all(u.startswith("https://site.example/") for u in urls), kind
        assert not [b for row in plain.buttons for b in row if b.url], kind  # без https кнопок нет
        if kind != T.ACCOUNT_BLOCKED:
            assert (
                urls
                or kind == T.MEMBER_REMOVED
                or any(b.callback for row in linked.buttons for b in row)
            ), kind
    assert not n.render(T.ACCOUNT_BLOCKED, {}, "https://site.example").buttons


def test_http_site_url_gives_no_link_buttons() -> None:
    rendered = n.render(T.REQUEST_REJECTED, SAMPLES[T.REQUEST_REJECTED], "http://localhost:8000")
    assert rendered.buttons == []


def test_user_text_is_escaped() -> None:
    payload = {**SAMPLES[T.NEW_JOIN_REQUEST], "title": "<b>X</b>", "applicant_username": "a<i>"}
    text = n.render(T.NEW_JOIN_REQUEST, payload).text
    assert "<b>" not in text and "&lt;b&gt;" in text and "@a&lt;i&gt;" in text
    removed = {**SAMPLES[T.MEMBER_REMOVED], "reason": "<script>"}
    assert "<script>" not in n.render(T.MEMBER_REMOVED, removed).text


def test_callbacks_match_bot_handlers() -> None:
    assert n.DECIDE_OK == f"{my_groups.APPLICATION_DECIDE_PREFIX}ok:"
    assert n.DECIDE_NO == f"{my_groups.APPLICATION_DECIDE_PREFIX}no:"
    assert n.REPORT_NEW == my_groups.REPORT


def test_approved_text_has_contact_and_date() -> None:
    text = n.render(T.REQUEST_APPROVED, SAMPLES[T.REQUEST_APPROVED], "", date(2026, 6, 1)).text
    assert "@alice" in text and "07.06.2026" in text and "933.34 ₸" in text


async def test_reminders_go_through_the_queue_once(session_maker) -> None:
    from bot.services import reminders as r
    from tests.test_reminders import make_group

    group = await make_group(session_maker, billing_day=10)
    assert await r.enqueue_daily_reminders(session_maker, date(2026, 6, 9)) == 3
    assert await r.enqueue_daily_reminders(session_maker, date(2026, 6, 9)) == 0  # повтор
    events = await outbox(session_maker, T.PAYMENT_REMINDER)
    assert sorted(e.payload["role"] for e in events) == ["member", "member", "owner"]
    assert all(
        e.payload["group_id"] == group.id and e.payload["when"] == "tomorrow" for e in events
    )
