import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass

MAX_AUTH_AGE = 300  # секунд: данные входа старше считаются устаревшими
MAX_CLOCK_SKEW = 60  # допуск на часы Telegram, убегающие вперёд


class TelegramAuthError(Exception):
    """Данные входа не прошли проверку. Текст можно показывать пользователю."""


@dataclass(frozen=True)
class TelegramProfile:
    telegram_id: int
    username: str | None
    first_name: str | None


def _data_check_string(fields: Mapping[str, str]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields))


def verify_telegram_login(
    data: Mapping[str, str],
    bot_token: str,
    *,
    now: float | None = None,
    max_age: int = MAX_AUTH_AGE,
) -> TelegramProfile:
    """Проверяет подпись Telegram Login Widget: HMAC-SHA256, ключ = SHA256(токен бота)."""
    fields = dict(data)
    received = fields.pop("hash", "")
    if not received:
        raise TelegramAuthError("Нет подписи Telegram.")
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, _data_check_string(fields).encode(), hashlib.sha256).hexdigest()
    # Сравниваем байты: compare_digest для str падает на не-ASCII
    if not hmac.compare_digest(expected.encode(), received.encode()):
        raise TelegramAuthError("Подпись Telegram не прошла проверку.")
    try:
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        telegram_id = int(fields["id"])
    except (KeyError, ValueError):
        raise TelegramAuthError("Некорректные данные входа.") from None
    if age > max_age or age < -MAX_CLOCK_SKEW:
        raise TelegramAuthError("Данные входа устарели, попробуйте войти ещё раз.")
    return TelegramProfile(telegram_id, fields.get("username") or None, fields.get("first_name"))
