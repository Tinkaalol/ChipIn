import hashlib
import hmac
from functools import lru_cache

from shared.config import SharedSettings


class BackendSettings(SharedSettings):
    # Токен нужен только для проверки подписи Telegram Login Widget
    bot_token: str
    bot_username: str = ""  # без @; домен сайта должен быть привязан к боту через /setdomain
    session_secret: str = ""
    cookie_secure: bool = True  # для локальной разработки по http: COOKIE_SECURE=false
    public_url: str = ""  # https://example.com, нужен виджету входа за прокси

    @property
    def secret_key(self) -> str:
        if self.session_secret:
            return self.session_secret
        # Без явного секрета выводим ключ из токена бота, чтобы сессии не были без подписи
        return hmac.new(self.bot_token.encode(), b"web-session", hashlib.sha256).hexdigest()


@lru_cache
def get_backend_settings() -> BackendSettings:
    return BackendSettings()  # type: ignore[call-arg]
