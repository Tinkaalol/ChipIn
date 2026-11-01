import pytest

from bot.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite+aiosqlite:///./bot.db", "sqlite+aiosqlite:///./bot.db"),
    ],
)
def test_database_url_gets_async_driver(raw: str, expected: str) -> None:
    assert Settings(bot_token="1:T", database_url=raw, _env_file=None).database_url == expected
