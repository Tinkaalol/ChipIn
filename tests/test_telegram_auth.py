import hashlib
import hmac

import pytest

from backend.auth import MAX_AUTH_AGE, TelegramAuthError, verify_telegram_login

TOKEN = "123456:TEST-token"
NOW = 1_800_000_000


def signed(**overrides: str) -> dict[str, str]:
    fields = {
        "id": "42",
        "first_name": "Али",
        "username": "ali",
        "auth_date": str(NOW - 10),
        **overrides,
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


def test_valid_signature() -> None:
    profile = verify_telegram_login(signed(), TOKEN, now=NOW)
    assert (profile.telegram_id, profile.username, profile.first_name) == (42, "ali", "Али")


def test_wrong_hash() -> None:
    data = signed()
    data["hash"] = "0" * 64
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(data, TOKEN, now=NOW)


def test_tampered_field_and_foreign_token_and_missing_hash() -> None:
    data = signed()
    data["id"] = "43"  # подмена пользователя при той же подписи
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(data, TOKEN, now=NOW)
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(signed(), "999:OTHER", now=NOW)
    data = signed()
    del data["hash"]
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(data, TOKEN, now=NOW)


def test_non_ascii_hash_is_rejected_not_crashing() -> None:
    data = signed()
    data["hash"] = "я" * 64
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(data, TOKEN, now=NOW)


def test_stale_auth_date() -> None:
    data = signed(auth_date=str(NOW - MAX_AUTH_AGE - 1))
    with pytest.raises(TelegramAuthError, match="устарели"):
        verify_telegram_login(data, TOKEN, now=NOW)
    # граница включительно: ровно 5 минут ещё допустимо
    verify_telegram_login(signed(auth_date=str(NOW - MAX_AUTH_AGE)), TOKEN, now=NOW)


def test_auth_date_from_the_future_is_rejected() -> None:
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(signed(auth_date=str(NOW + 3600)), TOKEN, now=NOW)


def test_garbage_fields_after_valid_signature() -> None:
    with pytest.raises(TelegramAuthError):
        verify_telegram_login(signed(id="abc"), TOKEN, now=NOW)
