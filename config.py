from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    supabase_url: str
    supabase_key: str
    admin_id: int = 0  # Telegram ID администратора; 0 — не задан
    kaspi_payment_info: str = ""
    support_contact: str = ""  # например @chipin_support; иначе ссылка на профиль админа


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
