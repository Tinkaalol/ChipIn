import calendar
from datetime import date, datetime, timedelta, timezone

ALMATY = timezone(timedelta(hours=5))  # UTC+5 круглый год
MAX_DAYS_AHEAD = 370


def today() -> date:
    return datetime.now(ALMATY).date()


def parse_billing_date(text: str, now: date) -> date:
    """Дата следующего списания в формате ГГГГ-ММ-ДД. ValueError с текстом для пользователя."""
    try:
        parsed = datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД, например 2026-12-05.") from None
    if parsed < now:
        raise ValueError("Эта дата уже прошла. Укажите ближайшую дату списания.")
    if parsed > now + timedelta(days=MAX_DAYS_AHEAD):
        raise ValueError("Слишком далеко: укажите дату в пределах года.")
    return parsed


def next_charge_date(billing: date, now: date) -> date:
    """Ближайшее списание не раньше сегодняшнего дня: подписка месячная, дата повторяется.

    Если в месяце меньше дней, чем в исходной дате, списание в последний день месяца.
    """
    for months in range(0, 1200):
        index = billing.month - 1 + months
        year, month = billing.year + index // 12, index % 12 + 1
        candidate = date(year, month, min(billing.day, calendar.monthrange(year, month)[1]))
        if candidate >= now:
            return candidate
    return billing
