from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from bot import keyboards
from bot.texts import WELCOME
from shared.config import get_shared_settings
from shared.db.session import session_maker
from shared.services.telegram_link import START_PREFIX, LinkError, link_telegram
from shared.services.users import register_user

router = Router(name="start")


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(START_PREFIX)))
async def cmd_start_link(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Переход по ссылке «Привязать Telegram» с сайта: /start link_<токен>."""
    await state.clear()
    user = message.from_user
    if user is None or not command.args:
        return
    try:
        await link_telegram(
            session_maker, command.args.removeprefix(START_PREFIX), user.id, user.username
        )
    except LinkError as e:
        await message.answer(str(e))
        return
    site = get_shared_settings().public_url
    markup = keyboards.url_button("Открыть сайт", site) if site.startswith("https://") else None
    await message.answer("Аккаунт привязан. Уведомления теперь приходят сюда.", reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is not None:
        await register_user(session_maker, user.id, user.username)
    # Сбрасываем старую Reply-клавиатуру, оставшуюся в чате от прошлых версий бота
    cleanup = await message.answer("…", reply_markup=ReplyKeyboardRemove())
    with suppress(TelegramAPIError):
        await cleanup.delete()
    await message.answer(WELCOME, reply_markup=keyboards.main_menu())


@router.callback_query(F.data == keyboards.HOME)
async def go_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(WELCOME, reply_markup=keyboards.main_menu())
    await callback.answer()
