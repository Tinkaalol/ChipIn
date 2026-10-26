from sqlalchemy import func, select

from bot.db.models import User
from bot.services.users import register_user


async def test_creates_user(session_maker) -> None:
    user = await register_user(session_maker, 42, "alice")
    assert user.id is not None
    assert user.username == "alice"


async def test_updates_username_without_duplicate(session_maker) -> None:
    await register_user(session_maker, 42, "alice")
    await register_user(session_maker, 42, "alice2")
    async with session_maker() as s:
        count = await s.scalar(select(func.count()).select_from(User))
        user = await s.scalar(select(User))
    assert count == 1
    assert user.username == "alice2"
