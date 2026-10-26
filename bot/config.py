from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    database_url: str = "sqlite+aiosqlite:///./bot.db"
    admin_ids: Annotated[list[int], NoDecode] = []

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        # В .env ID записаны через запятую одной строкой
        if isinstance(value, str):
            return [int(x) for x in value.split(",") if x.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
