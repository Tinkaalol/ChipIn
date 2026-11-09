"""Воркер очереди уведомлений: берёт события из notification_outbox и шлёт их в Telegram.

Работает внутри процесса бота. Два воркера не отправят одно событие дважды: строки
выбираются с FOR UPDATE SKIP LOCKED и тут же занимаются «арендой» (next_attempt_at).
Тексты сообщений в логи не попадают.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

from shared.db.models import NotificationOutbox, NotificationStatus, User
from shared.notifications import Rendered, render
from shared.services.groups import SessionMaker

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5.0  # секунд между проходами, если очередь пуста
BATCH_SIZE = 30
MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=5)  # пока событие в работе, другой воркер его не берёт
BASE_BACKOFF = timedelta(seconds=30)  # задержка после первой неудачи, дальше удваивается
GLOBAL_PER_SECOND = 25  # лимит Telegram: около 30 сообщений в секунду на бота
CHAT_INTERVAL = 1.0  # и не чаще одного сообщения в секунду в один чат
SHUTDOWN_TIMEOUT = 60.0

Outcome = str  # "sent" | "failed" | "skipped" | "retry"


class SendThrottle:
    """Ограничивает скорость отправки: не больше N сообщений в секунду всего и 1 в секунду в чат."""

    def __init__(
        self,
        per_second: int = GLOBAL_PER_SECOND,
        chat_interval: float = CHAT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.per_second = per_second
        self.chat_interval = chat_interval
        self.clock = clock
        self.sleep = sleep
        self._recent: deque[float] = deque()
        self._last_in_chat: dict[int, float] = {}
        self._paused_until = 0.0

    def wait_time(self, chat_id: int) -> float:
        """Сколько секунд ждать перед отправкой в этот чат (0 — можно слать сейчас)."""
        now = self.clock()
        while self._recent and now - self._recent[0] >= 1.0:
            self._recent.popleft()
        waits = [self._paused_until - now]
        if len(self._recent) >= self.per_second:
            waits.append(self._recent[0] + 1.0 - now)
        last = self._last_in_chat.get(chat_id)
        if last is not None:
            waits.append(last + self.chat_interval - now)
        return max(0.0, *waits)

    def record(self, chat_id: int) -> None:
        now = self.clock()
        self._recent.append(now)
        self._last_in_chat[chat_id] = now
        if len(self._last_in_chat) > 1000:  # не копим чаты, по которым ограничение уже прошло
            self._last_in_chat = {
                c: t for c, t in self._last_in_chat.items() if now - t < self.chat_interval
            }

    def pause(self, seconds: float) -> None:
        """Telegram ответил 429: притормаживаем всю отправку."""
        self._paused_until = max(self._paused_until, self.clock() + seconds)


@dataclass
class _Item:
    event_id: int
    type: str
    payload: dict
    attempts: int
    user_id: int
    chat_id: int | None
    notify_enabled: bool


def _markup(rendered: Rendered) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(text=b.text, url=b.url, callback_data=b.callback) for b in row]
        for row in rendered.buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def claim_batch(session_maker: SessionMaker, limit: int = BATCH_SIZE) -> list[_Item]:
    """Забирает до `limit` готовых событий. Параллельный воркер получит другие строки."""
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        ids = list(
            await session.scalars(
                select(NotificationOutbox.id)
                .where(
                    NotificationOutbox.status == NotificationStatus.PENDING,
                    NotificationOutbox.next_attempt_at <= now,
                )
                .order_by(NotificationOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)  # SQLite эту часть игнорирует
            )
        )
        if not ids:
            return []
        # Условный UPDATE: вторая линия защиты там, где SKIP LOCKED нет (SQLite)
        claimed = list(
            await session.scalars(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id.in_(ids),
                    NotificationOutbox.status == NotificationStatus.PENDING,
                    NotificationOutbox.next_attempt_at <= now,
                )
                .values(attempts=NotificationOutbox.attempts + 1, next_attempt_at=now + LEASE)
                .returning(NotificationOutbox.id)
            )
        )
        rows = await session.execute(
            select(NotificationOutbox, User)
            .join(User, NotificationOutbox.user_id == User.id)
            .where(NotificationOutbox.id.in_(claimed))
            .order_by(NotificationOutbox.id)
        )
        return [
            _Item(e.id, e.type, e.payload, e.attempts, u.id, u.telegram_id, u.notify_enabled)
            for e, u in rows.all()
        ]


async def _finish(
    session_maker: SessionMaker, item: _Item, disable_notifications: bool = False, **values: object
) -> None:
    async with session_maker() as session, session.begin():
        await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id == item.event_id)
            .values(**values)
        )
        if disable_notifications:
            await session.execute(
                update(User).where(User.id == item.user_id).values(notify_enabled=False)
            )


def _recipient_is_gone(error: TelegramAPIError) -> bool:
    """Бот заблокирован, чат не найден или аккаунт удалён: писать этому человеку нельзя."""
    text = str(error).lower()
    if isinstance(error, TelegramForbiddenError):
        return True  # blocked by the user / user is deactivated / can't initiate conversation
    return isinstance(error, TelegramBadRequest) and "chat not found" in text


async def _send(
    bot: Bot, session_maker: SessionMaker, item: _Item, site_url: str, throttle: SendThrottle | None
) -> Outcome:
    assert item.chat_id is not None
    now = datetime.now(UTC)
    try:
        rendered = render(item.type, item.payload, site_url)
        if throttle is not None:
            throttle.record(item.chat_id)
        await bot.send_message(
            item.chat_id, rendered.text, reply_markup=_markup(rendered), parse_mode="HTML"
        )
    except TelegramRetryAfter as error:
        if throttle is not None:
            throttle.pause(error.retry_after)
        return await _retry(session_maker, item, error, now, floor=error.retry_after)
    except TelegramAPIError as error:
        if _recipient_is_gone(error):
            reason = f"Получатель недоступен: {str(error)[:300]}"
            await _finish(
                session_maker,
                item,
                disable_notifications=True,
                status=NotificationStatus.SKIPPED,
                error=reason,
            )
            return "skipped"
        if isinstance(error, TelegramBadRequest | TelegramForbiddenError):
            # Сообщение некорректно (разметка, адрес кнопки): повтор не поможет
            await _finish(
                session_maker, item, status=NotificationStatus.FAILED, error=str(error)[:500]
            )
            return "failed"
        return await _retry(session_maker, item, error, now)  # сетевые и серверные сбои
    except (OSError, TimeoutError) as error:
        return await _retry(session_maker, item, error, now)
    await _finish(session_maker, item, status=NotificationStatus.SENT, sent_at=now, error=None)
    return "sent"


async def _retry(
    session_maker: SessionMaker, item: _Item, error: Exception, now: datetime, floor: float = 0
) -> Outcome:
    """attempts уже увеличен при захвате; после MAX_ATTEMPTS событие становится failed."""
    if item.attempts >= MAX_ATTEMPTS:
        await _finish(session_maker, item, status=NotificationStatus.FAILED, error=str(error)[:500])
        return "failed"
    delay = max(BASE_BACKOFF.total_seconds() * 2 ** (item.attempts - 1), floor)
    await _finish(
        session_maker,
        item,
        next_attempt_at=now + timedelta(seconds=delay),
        error=str(error)[:500],
    )
    return "retry"


async def _skip_unreachable(session_maker: SessionMaker, item: _Item) -> None:
    # Получатель мог отвязать Telegram или выключить уведомления после постановки в очередь
    await _finish(
        session_maker,
        item,
        status=NotificationStatus.SKIPPED,
        error="Telegram не привязан или уведомления выключены",
    )


async def process_outbox(
    bot: Bot,
    session_maker: SessionMaker,
    site_url: str = "",
    throttle: SendThrottle | None = None,
) -> dict[str, int]:
    """Один проход: забирает пачку и отправляет её целиком. Возвращает счётчики по исходам."""
    stats = {"sent": 0, "failed": 0, "skipped": 0, "retry": 0}
    queue: list[_Item] = []
    disabled: set[int] = set()  # кому в этом проходе отключили уведомления
    for item in await claim_batch(session_maker):
        if item.chat_id is None or not item.notify_enabled:
            await _skip_unreachable(session_maker, item)
            _log(item, "skipped")
            stats["skipped"] += 1
        else:
            queue.append(item)
    while queue:
        item = _next_ready(queue, throttle)
        if item is None:
            # Все получатели «остывают»: ждём ближайшего, остальные тем временем не блокируем
            assert throttle is not None
            await throttle.sleep(min(throttle.wait_time(i.chat_id) for i in queue))  # type: ignore[arg-type]
            continue
        queue.remove(item)
        if item.user_id in disabled:
            await _skip_unreachable(session_maker, item)
            _log(item, "skipped")
            stats["skipped"] += 1
            continue
        try:
            outcome = await _send(bot, session_maker, item, site_url, throttle)
        except Exception:
            # Неожиданный сбой не должен ронять воркер; событие вернётся после LEASE
            logger.exception("Сбой при отправке уведомления %s", item.event_id)
            continue
        _log(item, outcome)
        stats[outcome] += 1
        if outcome == "skipped":
            disabled.add(item.user_id)
    return stats


def _next_ready(queue: list[_Item], throttle: SendThrottle | None) -> _Item | None:
    if throttle is None:
        return queue[0]
    return next((i for i in queue if throttle.wait_time(i.chat_id) <= 0), None)  # type: ignore[arg-type]


def _log(item: _Item, outcome: Outcome) -> None:
    # Без текста сообщения: только служебные поля
    logger.info(
        "Уведомление %s тип=%s пользователь=%s попытка=%s итог=%s",
        item.event_id,
        item.type,
        item.user_id,
        item.attempts,
        outcome,
    )


async def run_outbox(
    bot: Bot,
    session_maker: SessionMaker,
    site_url: str = "",
    stop: asyncio.Event | None = None,
) -> None:
    """Цикл воркера. По stop доделывает текущую пачку и выходит; без stop работает до отмены."""
    stop = stop or asyncio.Event()
    throttle = SendThrottle()
    while not stop.is_set():
        try:
            stats = await process_outbox(bot, session_maker, site_url, throttle)
        except Exception:
            logger.exception("Сбой прохода по очереди уведомлений")
            stats = {}
        if sum(stats.values()) >= BATCH_SIZE:
            continue  # очередь, возможно, не пуста: следующий проход сразу
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), POLL_INTERVAL)
