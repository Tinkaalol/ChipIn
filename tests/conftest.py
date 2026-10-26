import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

os.environ.setdefault("BOT_TOKEN", "1:TEST")

from bot.db.models import Base  # noqa: E402
from bot.db.session import make_engine, make_session_maker  # noqa: E402


@pytest_asyncio.fixture
async def session_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_maker(engine)
    await engine.dispose()
