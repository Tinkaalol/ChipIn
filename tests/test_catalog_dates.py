from datetime import date

import pytest

from catalog import CATALOG, get_service, title_of
from dates import next_charge_date, parse_billing_date

NOW = date(2026, 11, 18)


def test_catalog_has_the_four_fixed_services() -> None:
    got = [(s.title, s.total_price, s.price_per_slot, s.max_slots) for s in CATALOG]
    assert got == [
        ("Яндекс Плюс", 3400, 850, 4),
        ("YouTube Premium", 5400, 900, 6),
        ("ChatGPT Team", 25000, 6250, 4),
        ("Google One (2 ТБ)", 4800, 800, 6),
    ]
    assert all(s.price_per_slot * s.max_slots == s.total_price for s in CATALOG)
    assert get_service("yandex_plus").price_per_slot == 850  # type: ignore[union-attr]
    assert get_service("nope") is None and title_of("netflix") == "netflix"


@pytest.mark.parametrize("text", ["2026-12-05", " 2026-12-05 ", "2026-11-18", "2027-11-01"])
def test_valid_billing_dates(text: str) -> None:
    assert parse_billing_date(text, NOW) == date.fromisoformat(text.strip())


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("05.12.2026", "формате"),
        ("2026-13-01", "формате"),
        ("2026-02-30", "формате"),
        ("завтра", "формате"),
        ("", "формате"),
        ("2026-11-17", "прошла"),
        ("2028-01-01", "Слишком далеко"),
    ],
)
def test_invalid_billing_dates(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_billing_date(text, NOW)


@pytest.mark.parametrize(
    ("billing", "now", "expected"),
    [
        (date(2026, 12, 5), date(2026, 11, 18), date(2026, 12, 5)),  # ещё впереди
        (date(2026, 11, 18), date(2026, 11, 18), date(2026, 11, 18)),  # сегодня
        (date(2026, 10, 5), date(2026, 11, 18), date(2026, 12, 5)),  # месячный перенос
        (date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 28)),  # короткий месяц
        (date(2028, 1, 31), date(2028, 2, 10), date(2028, 2, 29)),  # високосный год
        (date(2026, 1, 31), date(2026, 3, 1), date(2026, 3, 31)),  # день возвращается
        (date(2026, 11, 20), date(2026, 12, 25), date(2027, 1, 20)),  # смена года
    ],
)
def test_next_charge_date(billing: date, now: date, expected: date) -> None:
    assert next_charge_date(billing, now) == expected
