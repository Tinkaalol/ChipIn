import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import User

logger = logging.getLogger(__name__)


async def register_user(
    session_maker: async_sessionmaker[AsyncSession], telegram_id: int, username: str | None
) -> User:
    """Создаёт пользователя при первом /start, иначе обновляет username."""
    async with session_maker() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            logger.info("Новый пользователь %s", telegram_id)
        else:
            user.username = username
        await session.commit()
        return user
