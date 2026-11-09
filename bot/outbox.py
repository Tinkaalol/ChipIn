"""Воркер очереди уведомлений: берёт события из notification_outbox и шлёт их в Telegram."""

import asyncio
import logging
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

BATCH_SIZE = 20
MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=2)  # пока событие в работе, другой воркер его не берёт
BASE_BACKOFF = timedelta(seconds=30)
POLL_INTERVAL = 3.0


def _markup(rendered: Rendered) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(text=b.text, url=b.url, callback_data=b.callback) for b in row]
        for row in rendered.buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _claim(session_maker: SessionMaker, event_id: int) -> bool:
    """Занимает событие: счётчик попыток растёт, следующая попытка откладывается на LEASE."""
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        result = await session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == event_id,
                NotificationOutbox.status == NotificationStatus.PENDING,
                NotificationOutbox.next_attempt_at <= now,
            )
            .values(attempts=NotificationOutbox.attempts + 1, next_attempt_at=now + LEASE)
        )
        return result.rowcount == 1


async def _finish(session_maker: SessionMaker, event_id: int, **values: object) -> None:
    async with session_maker() as session, session.begin():
        await session.execute(
            update(NotificationOutbox).where(NotificationOutbox.id == event_id).values(**values)
        )


async def _deliver(bot: Bot, session_maker: SessionMaker, event_id: int, site_url: str) -> str:
    """Отправляет одно занятое событие и возвращает итоговый статус."""
    async with session_maker() as session:
        event = await session.get(NotificationOutbox, event_id)
        user = await session.get(User, event.user_id)  # type: ignore[union-attr]
    now = datetime.now(UTC)
    # Получатель мог отвязать Telegram или выключить уведомления после постановки в очередь
    if user is None or user.telegram_id is None or not user.notify_enabled:
        await _finish(
            session_maker,
            event_id,
            status=NotificationStatus.SKIPPED,
            error="Telegram не привязан или уведомления выключены",
        )
        return "skipped"
    try:
        rendered = render(event.type, event.payload, site_url)  # type: ignore[union-attr]
        await bot.send_message(
            user.telegram_id, rendered.text, reply_markup=_markup(rendered), parse_mode="HTML"
        )
    except (TelegramForbiddenError, TelegramBadRequest) as error:
        # Бот заблокирован, чат не найден или сообщение некорректно: повтор не поможет
        logger.warning("Уведомление %s не доставить: %s", event_id, error)
        await _finish(
            session_maker, event_id, status=NotificationStatus.FAILED, error=str(error)[:500]
        )
        return "failed"
    except (TelegramAPIError, OSError, TimeoutError) as error:
        return await _retry(session_maker, event_id, event.attempts, error, now)  # type: ignore[union-attr]
    await _finish(session_maker, event_id, status=NotificationStatus.SENT, sent_at=now, error=None)
    return "sent"


async def _retry(
    session_maker: SessionMaker, event_id: int, attempts: int, error: Exception, now: datetime
) -> str:
    if attempts >= MAX_ATTEMPTS:
        logger.error("Уведомление %s не отправлено за %s попыток: %s", event_id, attempts, error)
        await _finish(
            session_maker, event_id, status=NotificationStatus.FAILED, error=str(error)[:500]
        )
        return "failed"
    if isinstance(error, TelegramRetryAfter):
        delay = timedelta(seconds=error.retry_after)
    else:
        delay = BASE_BACKOFF * 2 ** (attempts - 1)
    await _finish(session_maker, event_id, next_attempt_at=now + delay, error=str(error)[:500])
    return "retry"


async def process_outbox(
    bot: Bot, session_maker: SessionMaker, site_url: str = ""
) -> dict[str, int]:
    """Один проход: отправляет готовые события. Возвращает счётчики по исходам."""
    stats = {"sent": 0, "failed": 0, "skipped": 0, "retry": 0}
    async with session_maker() as session:
        ids = list(
            await session.scalars(
                select(NotificationOutbox.id)
                .where(
                    NotificationOutbox.status == NotificationStatus.PENDING,
                    NotificationOutbox.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(NotificationOutbox.id)
                .limit(BATCH_SIZE)
            )
        )
    for event_id in ids:
        if not await _claim(session_maker, event_id):
            continue  # событие уже взял другой воркер
        try:
            outcome = await _deliver(bot, session_maker, event_id, site_url)
        except Exception:
            # Неожиданный сбой не должен ронять воркер; событие вернётся после LEASE
            logger.exception("Сбой при отправке уведомления %s", event_id)
            continue
        stats[outcome] += 1
    return stats


async def run_outbox(bot: Bot, session_maker: SessionMaker, site_url: str = "") -> None:
    """Бесконечный цикл воркера; останавливается отменой задачи."""
    while True:
        try:
            processed = await process_outbox(bot, session_maker, site_url)
        except Exception:
            logger.exception("Сбой прохода по очереди уведомлений")
            processed = {}
        # Если очередь не пуста, не ждём между проходами
        if not any(processed.values()):
            await asyncio.sleep(POLL_INTERVAL)
