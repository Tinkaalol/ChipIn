import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


async def show(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None, *, html: bool = False
) -> None:
    """Заменяет сообщение с кнопкой на новый экран."""
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, reply_markup=markup, parse_mode="HTML" if html else None
        )
    await callback.answer()


async def notify(
    bot: Bot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    html: bool = False,
) -> bool:
    """Пишет другому пользователю; если он заблокировал бота, не роняет обработчик."""
    try:
        await bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode="HTML" if html else None
        )
    except TelegramAPIError:
        logger.warning("Не удалось отправить сообщение пользователю %s", chat_id, exc_info=True)
        return False
    return True
