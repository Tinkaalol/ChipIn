from html import escape

from shared.formatting import money as money

WELCOME = (
    "Привет! Я помогу найти компанию для совместной оплаты подписок.\n"
    "Деньги я не принимаю: участники переводят их друг другу сами."
)


def contact_html(telegram_id: int, username: str | None) -> str:
    """Контакт для сообщений с parse_mode=HTML: @username или ссылка на профиль."""
    if username:
        return f"@{escape(username)}"
    return f'<a href="tg://user?id={telegram_id}">профиль в Telegram</a>'
