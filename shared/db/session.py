from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shared.config import get_shared_settings


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url)


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


engine = make_engine(get_shared_settings().database_url)
session_maker = make_session_maker(engine)
