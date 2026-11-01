from decimal import Decimal

import pytest

from bot.config import Settings
from bot.services import catalog
from bot.services.groups import GroupError


async def test_seed_is_idempotent(session_maker) -> None:
    assert await catalog.seed_services(session_maker) == len(catalog.SERVICES)
    assert await catalog.seed_services(session_maker) == 0
    assert len(await catalog.list_all_services(session_maker)) == len(catalog.SERVICES)


async def test_service_toggle_and_price(session_maker) -> None:
    await catalog.seed_services(session_maker)
    service = (await catalog.list_all_services(session_maker))[0]
    await catalog.set_service_active(session_maker, service.id, False)
    assert service.id not in [s.id for s in await groups_active(session_maker)]
    updated = await catalog.set_service_price(session_maker, service.id, Decimal("350"))
    assert updated.price_month == Decimal("350")
    for func, args in (
        (catalog.set_service_active, (999, True)),
        (catalog.set_service_price, (999, Decimal("1"))),
    ):
        with pytest.raises(GroupError, match="не найден"):
            await func(session_maker, *args)


async def groups_active(session_maker):
    return await __import__("bot.services.groups", fromlist=["x"]).list_active_services(
        session_maker
    )


async def test_add_service_validation(session_maker) -> None:
    price = Decimal("10")
    with pytest.raises(GroupError, match="пустыми"):
        await catalog.add_service(session_maker, " ", "Plan", price, 3)
    with pytest.raises(GroupError, match="от 2 до"):
        await catalog.add_service(session_maker, "A", "B", price, 21)
    await catalog.add_service(session_maker, "A", "B", price, 20)


@pytest.mark.parametrize("raw", ["0", "1000000.01", "1.999", "nan", "inf", "", "1e3x"])
def test_parse_price_rejects(raw: str) -> None:
    with pytest.raises(GroupError):
        catalog.parse_price(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1, 2", [1, 2]), ("", []), ("5", [5]), (" 7 ,8,", [7, 8])],
)
def test_admin_ids_parsing(raw: str, expected: list[int]) -> None:
    settings = Settings(bot_token="1:T", admin_ids=raw, _env_file=None)
    assert settings.admin_ids == expected
