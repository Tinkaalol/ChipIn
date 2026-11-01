import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import User

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
            user = User(telegram_id=telegram_id, username=username)
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
            await session.commit()
        return user
