import logging

logger = logging.getLogger(__name__)


async def register_user(telegram_id: int, username: str | None) -> None:
    """Заглушка: реальная запись в БД появится вместе с моделями."""
    logger.info("Регистрация пользователя %s (@%s)", telegram_id, username)
