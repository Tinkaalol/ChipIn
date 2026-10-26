from decimal import Decimal

import pytest

from bot.db.models import GroupStatus, Service, User
from bot.services import groups as svc


def test_calc_price_exact() -> None:
    assert svc.calc_price_per_member(Decimal("300"), 6) == Decimal("50.00")


def test_calc_price_rounds_up_to_kopeck() -> None:
    # 100 / 3 = 33.333... -> 33.34
    assert svc.calc_price_per_member(Decimal("100"), 3) == Decimal("33.34")
    # 0.01 / 2 = 0.005 -> 0.01
    assert svc.calc_price_per_member(Decimal("0.01"), 2) == Decimal("0.01")


def test_calc_price_zero_slots() -> None:
    with pytest.raises(svc.GroupError):
        svc.calc_price_per_member(Decimal("100"), 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("299", "299.00"), (" 299,5 ", "299.50"), ("1500.00", "1500.00"), ("299 ₽", "299.00")],
)
def test_parse_price_ok(text: str, expected: str) -> None:
    assert svc.parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "1.234", "100001", "nan", "inf"])
def test_parse_price_bad(text: str) -> None:
    with pytest.raises(svc.GroupError):
        svc.parse_price(text)


def test_parse_int() -> None:
    assert svc.parse_int(" 7 ") == 7
    for bad in ("", "x", "1.5"):
        with pytest.raises(svc.GroupError):
            svc.parse_int(bad)


def test_validate_slots_total_bounds() -> None:
    assert svc.validate_slots_total(2, 6) == 2
    assert svc.validate_slots_total(6, 6) == 6
    for bad in (1, 0, -1, 7):
        with pytest.raises(svc.GroupError):
            svc.validate_slots_total(bad, 6)


def test_validate_slots_free_bounds() -> None:
    assert svc.validate_slots_free(1, 5) == 1
    assert svc.validate_slots_free(4, 5) == 4
    for bad in (0, 5, -1):
        with pytest.raises(svc.GroupError):
            svc.validate_slots_free(bad, 5)


def test_validate_billing_day_bounds() -> None:
    assert svc.validate_billing_day(1) == 1
    assert svc.validate_billing_day(28) == 28
    for bad in (0, 29, 31, -3):
        with pytest.raises(svc.GroupError):
            svc.validate_billing_day(bad)


async def _setup(session_maker, max_members: int = 6) -> tuple[int, int]:
    async with session_maker() as s:
        user = User(telegram_id=1)
        service = Service(
            name="Spotify", plan_name="Family", price_month=Decimal("299"), max_members=max_members
        )
        s.add_all([user, service])
        await s.commit()
        return user.id, service.id


async def _create(session_maker, owner_id: int, service_id: int):
    return await svc.create_group(session_maker, owner_id, service_id, Decimal("300"), 6, 5, 10)


async def test_create_group_saves_calculated_price(session_maker) -> None:
    owner, service = await _setup(session_maker)
    group = await _create(session_maker, owner, service)
    assert group.price_per_member == Decimal("50.00")
    assert group.slots_free == 5
    assert group.status == GroupStatus.OPEN


async def test_limit_three_open_groups(session_maker) -> None:
    owner, service = await _setup(session_maker)
    for _ in range(svc.MAX_OPEN_GROUPS):
        await _create(session_maker, owner, service)
    with pytest.raises(svc.GroupLimitError):
        await _create(session_maker, owner, service)
    with pytest.raises(svc.GroupLimitError):
        await svc.ensure_can_create(session_maker, owner)


async def test_closed_group_frees_limit(session_maker) -> None:
    owner, service = await _setup(session_maker)
    groups = [await _create(session_maker, owner, service) for _ in range(3)]
    async with session_maker() as s:
        g = await s.get(type(groups[0]), groups[0].id)
        g.status = GroupStatus.CLOSED
        await s.commit()
    assert await svc.count_open_groups(session_maker, owner) == 2
    await _create(session_maker, owner, service)


async def test_limit_is_per_user(session_maker) -> None:
    owner, service = await _setup(session_maker)
    async with session_maker() as s:
        other = User(telegram_id=2)
        s.add(other)
        await s.commit()
        other_id = other.id
    for _ in range(3):
        await _create(session_maker, owner, service)
    await _create(session_maker, other_id, service)


async def test_create_group_rejects_invalid_and_inactive(session_maker) -> None:
    owner, service = await _setup(session_maker, max_members=4)
    with pytest.raises(svc.GroupError):
        await svc.create_group(session_maker, owner, service, Decimal("300"), 6, 5, 10)
    with pytest.raises(svc.GroupError):
        await svc.create_group(session_maker, owner, 999, Decimal("300"), 4, 3, 10)
    async with session_maker() as s:
        (await s.get(Service, service)).is_active = False
        await s.commit()
    with pytest.raises(svc.GroupError):
        await svc.create_group(session_maker, owner, service, Decimal("300"), 4, 3, 10)
