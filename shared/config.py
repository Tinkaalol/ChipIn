from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Локальный PostgreSQL из docker-compose.yml
DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/subscriptions"


class SharedSettings(BaseSettings):
    """Настройки, общие для backend и bot. Токен бота сюда не входит."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = DEFAULT_DATABASE_URL
    admin_ids: Annotated[list[int], NoDecode] = []
    public_url: str = ""  # адрес сайта, например https://example.com (кнопка в боте, виджет входа)

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        # Render отдаёт postgres:// или postgresql://, а SQLAlchemy async нужен драйвер asyncpg
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value.removeprefix(prefix)
        return value

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        # В .env ID записаны через запятую одной строкой
        if isinstance(value, str):
            return [int(x) for x in value.split(",") if x.strip()]
        return value


@lru_cache
def get_shared_settings() -> SharedSettings:
    return SharedSettings()
