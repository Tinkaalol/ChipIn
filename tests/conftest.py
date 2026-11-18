from datetime import date

import pytest

TODAY = date(2026, 11, 18)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """«Сегодня» в боте всегда 18.11.2026: тесты не зависят от даты запуска."""
    monkeypatch.setattr("dates.today", lambda: TODAY)
