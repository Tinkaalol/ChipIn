from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.db.session import session_maker
from bot.services.users import register_user

router = Router(name="start")

WELCOME = (
    "Привет! Я помогу найти компанию для совместной оплаты подписок.\n"
    "Деньги я не принимаю: участники переводят их друг другу сами."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if user is not None:
        await register_user(session_maker, user.id, user.username)
    await message.answer(WELCOME, reply_markup=keyboards.main_menu())


@router.callback_query(
    F.data.in_({keyboards.FIND_GROUP, keyboards.CREATE_GROUP, keyboards.MY_GROUPS})
)
async def soon(callback: CallbackQuery) -> None:
    await callback.answer("Скоро", show_alert=False)
