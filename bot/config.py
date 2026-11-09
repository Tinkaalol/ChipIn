from functools import lru_cache

from shared.config import SharedSettings


class Settings(SharedSettings):
    bot_token: str


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
