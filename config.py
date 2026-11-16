from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    supabase_url: str
    supabase_key: str
    admin_id: int = 0  # Telegram ID администратора; 0 — не задан
    kaspi_payment_info: str = ""
    support_contact: str = ""  # например @chipin_support; иначе ссылка на профиль админа

    @field_validator("supabase_url")
    @classmethod
    def _base_url_only(cls, value: str) -> str:
        # Часто вставляют адрес вида https://xxx.supabase.co/rest/v1/: клиенту нужен только корень
        parts = urlsplit(value.strip())
        if not parts.scheme or not parts.netloc:
            raise ValueError("SUPABASE_URL должен выглядеть как https://xxxx.supabase.co")
        return f"{parts.scheme}://{parts.netloc}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
