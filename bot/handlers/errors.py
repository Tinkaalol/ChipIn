import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot import keyboards
from bot.texts import WELCOME

router = Router(name="errors")
logger = logging.getLogger(__name__)

# Безобидные ответы Telegram: двойной клик по кнопке или кнопка из старого сообщения
HARMLESS = ("message is not modified", "query is too old", "query id is invalid")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and any(h in str(exc).lower() for h in HARMLESS):
        return True
    logger.exception("Ошибка при обработке обновления", exc_info=exc)
    return True


@router.callback_query()
async def stale_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Последний в цепочке: кнопка не подошла ни одному шагу (например, после перезапуска бота)."""
    if await state.get_state() is not None:
        # Диалог идёт, а нажата кнопка другого шага (например, двойной клик) — просто игнорируем
        await callback.answer()
        return
    await callback.answer("Эта кнопка устарела, возвращаю в меню.")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(WELCOME, reply_markup=keyboards.main_menu())
