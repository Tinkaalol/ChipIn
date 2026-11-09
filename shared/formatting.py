from decimal import Decimal

from shared.db.models import DEFAULT_CURRENCY

CURRENCY_SIGNS = {"KZT": "₸"}


def money(amount: Decimal | str, currency: str = DEFAULT_CURRENCY) -> str:
    return f"{Decimal(amount):.2f} {CURRENCY_SIGNS.get(currency, currency)}"
