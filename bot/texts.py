from decimal import Decimal
from html import escape

WELCOME = (
    "Привет! Я помогу найти компанию для совместной оплаты подписок.\n"
    "Деньги я не принимаю: участники переводят их друг другу сами."
)

CURRENCY_SIGNS = {"RUB": "₽"}


def money(amount: Decimal | str, currency: str = "RUB") -> str:
    return f"{Decimal(amount):.2f} {CURRENCY_SIGNS.get(currency, currency)}"


def contact_html(telegram_id: int, username: str | None) -> str:
    """Контакт для сообщений с parse_mode=HTML: @username или ссылка на профиль."""
    if username:
        return f"@{escape(username)}"
    return f'<a href="tg://user?id={telegram_id}">профиль в Telegram</a>'
