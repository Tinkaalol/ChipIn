import calendar
from datetime import date, timedelta, timezone

# Алматы: UTC+5 круглый год
ALMATY = timezone(timedelta(hours=5))


def effective_billing_day(billing_day: int, year: int, month: int) -> int:
    """Если в месяце меньше дней, чем billing_day, оплата в последний день месяца."""
    return min(billing_day, calendar.monthrange(year, month)[1])


def next_payment_date(billing_day: int, today: date) -> date:
    """Ближайшая дата оплаты, не раньше сегодняшнего дня."""
    this_month = date(
        today.year, today.month, effective_billing_day(billing_day, today.year, today.month)
    )
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, effective_billing_day(billing_day, year, month))
