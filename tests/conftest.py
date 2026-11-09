import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

os.environ.setdefault("BOT_TOKEN", "1:TEST")

from shared.db.models import Base  # noqa: E402
from shared.db.session import make_engine, make_session_maker  # noqa: E402


@pytest_asyncio.fixture
async def session_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # По умолчанию SQLite в памяти; TEST_DATABASE_URL=postgresql+asyncpg://... включает PostgreSQL
    engine = make_engine(os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_maker(engine)
    await engine.dispose()


ADMIN_ID = 777


@pytest_asyncio.fixture
async def file_session_maker(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """База в файле: нужна для проверки параллельных запросов (в памяти соединение одно)."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_maker(engine)
    await engine.dispose()


@pytest.fixture
def dp(session_maker, monkeypatch):
    """Диспетчер со всеми роутерами поверх тестовой базы."""
    from bot.handlers import admin, create_group, errors, my_groups, report, search, start
    from bot.main import build_dispatcher

    for module in (admin, create_group, my_groups, report, search, start):
        monkeypatch.setattr(f"{module.__name__}.session_maker", session_maker)
    monkeypatch.setattr("bot.handlers.admin.admin_ids", lambda: {ADMIN_ID})
    monkeypatch.setattr(
        "bot.handlers.report.get_settings", lambda: type("S", (), {"admin_ids": [ADMIN_ID]})()
    )
    # Роутеры модульные, поэтому перед новой сборкой диспетчера отцепляем их от старого
    for module in (admin, create_group, errors, my_groups, report, search, start):
        module.router._parent_router = None
    return build_dispatcher(session_maker)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Лимитеры глобальные, а в каждом тесте новая база с теми же id пользователей."""
    from bot.services.ratelimit import application_limiter, group_limiter

    application_limiter.clear()
    group_limiter.clear()
