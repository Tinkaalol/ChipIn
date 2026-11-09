import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import User

logger = logging.getLogger(__name__)


async def _by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def register_user(
    session_maker: async_sessionmaker[AsyncSession], telegram_id: int, username: str | None
) -> User:
    """Создаёт пользователя при первом обращении, иначе обновляет username."""
    async with session_maker() as session:
        user = await _by_telegram_id(session, telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                telegram_username=username,
                telegram_linked_at=datetime.now(UTC),
            )
            session.add(user)
            try:
                await session.commit()
            except IntegrityError:
                # Два первых запроса подряд: другой успел создать запись
                await session.rollback()
                user = await _by_telegram_id(session, telegram_id)
                if user is None:
                    raise
            else:
                logger.info("Новый пользователь %s", telegram_id)
                return user
        if user.username != username:
            user.username = username
            user.telegram_username = username
            await session.commit()
        return user


LANGUAGES = ("ru", "kk", "en")


def chat_id_for(user: User) -> int | None:
    """Куда слать уведомление: только привязанному Telegram и если уведомления включены."""
    return user.telegram_id if user.notify_enabled else None


async def set_language(
    session_maker: async_sessionmaker[AsyncSession], user_id: int, language: str
) -> None:
    if language not in LANGUAGES:
        raise ValueError("Неизвестный язык.")
    async with session_maker() as session:
        user = await session.get(User, user_id)
        if user is not None:
            user.language = language
            await session.commit()
