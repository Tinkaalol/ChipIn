"""Воркер очереди уведомлений с замоканным Telegram API (настоящий Bot, сеть подменена)."""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from bot import outbox as worker
from shared.db.models import NotificationOutbox, NotificationStatus, User
from tests.fakes import FakeSession, outbox

T = "account_blocked"  # самый короткий тип: {} в payload


class ScriptedSession(FakeSession):
    """Как FakeSession, но ошибки задаются сценарием: {chat_id: [исключение | None, ...]}."""

    def __init__(self, script: dict[int, list[Exception | None]] | None = None) -> None:
        super().__init__()
        self.script = script or {}
        self.sent: list[SendMessage] = []
        self.times: list[tuple[int, float]] = []
        self.clock = lambda: 0.0
        self.on_send = None

    async def make_request(self, bot, method, timeout=None):
        steps = self.script.get(method.chat_id) or []
        error = steps.pop(0) if steps else None
        if self.on_send:
            self.on_send()
        if error:
            self.calls.append(method)
            raise error
        self.sent.append(method)
        self.times.append((method.chat_id, self.clock()))
        return await super().make_request(bot, method, timeout)


def make_bot(script=None) -> tuple[Bot, ScriptedSession]:
    session = ScriptedSession(script)
    return Bot("1:TEST", session=session), session


def method_for(chat_id: int):
    return SendMessage(chat_id=chat_id, text="x")


def forbidden(chat_id: int) -> TelegramForbiddenError:
    return TelegramForbiddenError(method_for(chat_id), "Forbidden: bot was blocked by the user")


def chat_not_found(chat_id: int) -> TelegramBadRequest:
    return TelegramBadRequest(method_for(chat_id), "Bad Request: chat not found")


def too_many(chat_id: int, seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method_for(chat_id), "Too Many Requests", seconds)


async def add_events(sm, count: int, per_user: int = 1, base_chat: int = 1000) -> list[int]:
    """Пользователи с telegram_id и события в очереди. Возвращает telegram_id пользователей."""
    async with sm() as s:
        users = [User(telegram_id=base_chat + i, username=f"u{i}") for i in range(count)]
        s.add_all(users)
        await s.flush()
        for u in users:
            for _ in range(per_user):
                s.add(NotificationOutbox(user_id=u.id, type=T, payload={}))
        await s.commit()
        return [u.telegram_id for u in users]


async def event(sm, index: int = 0) -> NotificationOutbox:
    return (await outbox(sm))[index]


async def make_due(sm) -> None:
    async with sm() as s:
        await s.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.status == NotificationStatus.PENDING)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()


# --- успех и повторная отправка ---


async def test_success_marks_sent_and_is_not_resent(session_maker) -> None:
    (chat,) = await add_events(session_maker, 1)
    bot, session = make_bot()
    stats = await worker.process_outbox(bot, session_maker, "https://site.example")
    assert stats == {"sent": 1, "failed": 0, "skipped": 0, "retry": 0}
    row = await event(session_maker)
    assert row.status == NotificationStatus.SENT and row.sent_at is not None and row.attempts == 1
    assert [m.chat_id for m in session.sent] == [chat] and session.sent[0].parse_mode == "HTML"
    # ни следующий проход, ни ещё один воркер не отправляют то же событие
    await worker.process_outbox(bot, session_maker)
    await make_due(session_maker)
    await worker.process_outbox(bot, session_maker)
    assert len(session.sent) == 1


async def test_concurrent_workers_do_not_duplicate(file_session_maker) -> None:
    await add_events(file_session_maker, 10)
    bot, session = make_bot()
    await asyncio.gather(*(worker.process_outbox(bot, file_session_maker) for _ in range(4)))
    assert len(session.sent) == 10 and len({m.chat_id for m in session.sent}) == 10


async def test_batch_is_limited_to_30(session_maker) -> None:
    await add_events(session_maker, 35)
    bot, session = make_bot()
    await worker.process_outbox(bot, session_maker)
    assert len(session.sent) == worker.BATCH_SIZE == 30
    await worker.process_outbox(bot, session_maker)
    assert len(session.sent) == 35


async def test_not_yet_due_events_are_left_alone(session_maker) -> None:
    await add_events(session_maker, 1)
    async with session_maker() as s:
        await s.execute(
            update(NotificationOutbox).values(
                next_attempt_at=datetime.now(UTC) + timedelta(hours=1)
            )
        )
        await s.commit()
    bot, session = make_bot()
    assert not any((await worker.process_outbox(bot, session_maker)).values())
    assert session.sent == []


# --- временные ошибки ---


async def test_429_waits_at_least_retry_after(session_maker) -> None:
    (chat,) = await add_events(session_maker, 1)
    bot, session = make_bot({chat: [too_many(chat, 120)]})
    throttle = worker.SendThrottle()
    stats = await worker.process_outbox(bot, session_maker, "", throttle)
    assert stats["retry"] == 1
    row = await event(session_maker)
    assert row.status == NotificationStatus.PENDING and row.attempts == 1
    wait = row.next_attempt_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(seconds=110) < wait <= timedelta(seconds=120)
    assert throttle.wait_time(chat + 1) > 100  # отправка притормозилась целиком
    assert "Too Many" in row.error
    await make_due(session_maker)
    await worker.process_outbox(bot, session_maker)  # без троттлинга: тихо доходит
    assert (await event(session_maker)).status == NotificationStatus.SENT


async def test_exponential_backoff_then_failed_after_five_attempts(session_maker) -> None:
    (chat,) = await add_events(session_maker, 1)
    errors = [OSError("сеть") for _ in range(10)]
    bot, session = make_bot({chat: errors})
    delays = []
    for _ in range(worker.MAX_ATTEMPTS):
        before = datetime.now(UTC)
        await worker.process_outbox(bot, session_maker)
        row = await event(session_maker)
        if row.status == NotificationStatus.PENDING:
            delays.append(round((row.next_attempt_at.replace(tzinfo=UTC) - before).total_seconds()))
        await make_due(session_maker)
    assert delays == [30, 60, 120, 240]
    row = await event(session_maker)
    assert row.status == NotificationStatus.FAILED and row.attempts == 5 and "сеть" in row.error
    await worker.process_outbox(bot, session_maker)  # failed больше не берут
    assert len(session.calls) == 5


# --- получатель недоступен ---


@pytest.mark.parametrize("make_error", [forbidden, chat_not_found])
async def test_unreachable_recipient_disables_notifications(session_maker, make_error) -> None:
    (chat,) = await add_events(session_maker, 1, per_user=2)
    bot, session = make_bot({chat: [make_error(chat)]})
    stats = await worker.process_outbox(bot, session_maker)
    assert stats["skipped"] == 2 and stats["sent"] == 0
    first, second = await outbox(session_maker)
    assert first.status == second.status == NotificationStatus.SKIPPED
    assert "blocked" in first.error or "chat not found" in first.error
    async with session_maker() as s:
        assert (await s.scalar(select(User))).notify_enabled is False
    assert len(session.calls) == 1  # второе событие в Telegram даже не пошло


async def test_next_events_of_blocked_user_are_skipped_without_sending(session_maker) -> None:
    (chat,) = await add_events(session_maker, 1)
    bot, session = make_bot({chat: [forbidden(chat)]})
    await worker.process_outbox(bot, session_maker)
    async with session_maker() as s:  # новое событие для того же пользователя
        user = await s.scalar(select(User))
        s.add(NotificationOutbox(user_id=user.id, type=T, payload={}))
        await s.commit()
    await worker.process_outbox(bot, session_maker)
    rows = await outbox(session_maker)
    assert [r.status for r in rows] == [NotificationStatus.SKIPPED] * 2
    assert len(session.calls) == 1  # во второй раз в Telegram не ходили


async def test_broken_message_fails_without_disabling_user(session_maker) -> None:
    (chat,) = await add_events(session_maker, 1)
    bad = TelegramBadRequest(method_for(chat), "Bad Request: can't parse entities")
    bot, _ = make_bot({chat: [bad]})
    assert (await worker.process_outbox(bot, session_maker))["failed"] == 1
    async with session_maker() as s:
        assert (await s.scalar(select(User))).notify_enabled is True


# --- лимиты Telegram ---


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_global_limit_is_25_per_second(session_maker) -> None:
    await add_events(session_maker, 30)
    clock = Clock()
    bot, session = make_bot()
    session.clock = clock
    throttle = worker.SendThrottle(clock=clock, sleep=clock.sleep)
    await worker.process_outbox(bot, session_maker, "", throttle)
    per_second: dict[int, int] = {}
    for _, at in session.times:
        per_second[int(at)] = per_second.get(int(at), 0) + 1
    assert len(session.times) == 30 and max(per_second.values()) <= 25
    assert per_second[0] == 25 and clock.now >= 1.0


async def test_one_message_per_second_per_chat_without_blocking_others(session_maker) -> None:
    chats = await add_events(session_maker, 2, per_user=2)
    clock = Clock()
    bot, session = make_bot()
    session.clock = clock
    throttle = worker.SendThrottle(clock=clock, sleep=clock.sleep)
    await worker.process_outbox(bot, session_maker, "", throttle)
    by_chat: dict[int, list[float]] = {c: [] for c in chats}
    for chat, at in session.times:
        by_chat[chat].append(at)
    for times in by_chat.values():
        assert len(times) == 2 and times[1] - times[0] >= 1.0
    # пока первый чат «остывал», второй уже получил своё первое сообщение без ожидания
    assert sorted(at for _, at in session.times)[:2] == [0.0, 0.0]


def test_throttle_unit() -> None:
    clock = Clock()
    t = worker.SendThrottle(per_second=2, clock=clock, sleep=clock.sleep)
    assert t.wait_time(1) == 0
    t.record(1)
    assert t.wait_time(1) == 1.0 and t.wait_time(2) == 0
    t.record(2)
    assert t.wait_time(3) == 1.0  # окно на 2 сообщения заполнено
    clock.now = 1.0
    assert t.wait_time(1) == 0 and t.wait_time(3) == 0
    t.pause(5)
    assert t.wait_time(9) == 5


# --- логи и остановка ---


async def test_logs_every_send_without_message_text(session_maker, caplog) -> None:
    async with session_maker() as s:
        user = User(telegram_id=77, username="secret_nick")
        s.add(user)
        await s.flush()
        s.add(
            NotificationOutbox(
                user_id=user.id,
                type="new_join_request",
                payload={
                    "group_id": 1,
                    "membership_id": 1,
                    "title": "Spotify Family",
                    "slots_free": 1,
                    "applicant_username": "secret_nick",
                    "applicant_telegram_id": 5,
                },
            )
        )
        await s.commit()
    bot, session = make_bot()
    with caplog.at_level(logging.INFO, logger="bot.outbox"):
        await worker.process_outbox(bot, session_maker)
    assert "Новая заявка" in session.sent[0].text  # сообщение действительно ушло
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "итог=sent" in logged and "new_join_request" in logged
    assert "Новая заявка" not in logged and "secret_nick" not in logged and "Spotify" not in logged


async def test_shutdown_finishes_current_batch(session_maker) -> None:
    await add_events(session_maker, 5)
    stop = asyncio.Event()
    bot, session = make_bot()
    session.on_send = stop.set  # остановка приходит посреди пачки
    await asyncio.wait_for(worker.run_outbox(bot, session_maker, "", stop), 5)
    assert len(session.sent) == 5
    assert {e.status for e in await outbox(session_maker)} == {NotificationStatus.SENT}


async def test_run_loop_picks_up_new_events(session_maker, monkeypatch) -> None:
    monkeypatch.setattr(worker, "POLL_INTERVAL", 0.05)
    stop = asyncio.Event()
    bot, session = make_bot()
    task = asyncio.create_task(worker.run_outbox(bot, session_maker, "", stop))
    await asyncio.sleep(0.1)
    await add_events(session_maker, 1)
    for _ in range(40):
        if session.sent:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert len(session.sent) == 1


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="FOR UPDATE SKIP LOCKED есть только в PostgreSQL",
)
async def test_skip_locked_on_postgres_no_duplicates(session_maker) -> None:
    await add_events(session_maker, 60)
    bot, session = make_bot()
    await asyncio.gather(*(worker.process_outbox(bot, session_maker) for _ in range(6)))
    await worker.process_outbox(bot, session_maker)
    await worker.process_outbox(bot, session_maker)
    chats = [m.chat_id for m in session.sent]
    assert len(chats) == 60 and len(set(chats)) == 60
