from decimal import Decimal
from html import escape

from shared.db.models import DEFAULT_CURRENCY

CURRENCY_SIGNS = {"KZT": "₸"}


def money(amount: Decimal | str, currency: str = DEFAULT_CURRENCY) -> str:
    return f"{Decimal(amount):.2f} {CURRENCY_SIGNS.get(currency, currency)}"


def contact_html(telegram_id: int | None, username: str | None) -> str:
    """Контакт для сообщений с parse_mode=HTML: @username или ссылка на профиль."""
    if username:
        return f"@{escape(username)}"
    if telegram_id is None:
        return "пользователь сайта"
    return f'<a href="tg://user?id={telegram_id}">профиль в Telegram</a>'
