from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject, Update

from shared.services.groups import SessionMaker
from shared.services.moderation import is_banned

BANNED_TEXT = "Доступ к боту ограничен."


class BanMiddleware(BaseMiddleware):
    """Не пускает забаненных пользователей ни в один обработчик."""

    def __init__(self, session_maker: SessionMaker) -> None:
        self.session_maker = session_maker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or not await is_banned(self.session_maker, user.id):
            return await handler(event, data)
        if isinstance(event, Update):
            if event.callback_query is not None:
                await event.callback_query.answer(BANNED_TEXT, show_alert=True)
            elif event.message is not None:
                await event.message.answer(BANNED_TEXT)
        return None


# Состояния, где бот ждёт свободный текст, и кнопки, которые к ним относятся.
# Любая другая кнопка означает, что человек ушёл из диалога: состояние сбрасывается,
# иначе его следующее сообщение боту ушло бы как причина удаления, жалоба или цена.
TEXT_STATES: dict[str, tuple[str, ...]] = {
    "ReportFlow:comment": ("r:",),
    "RemoveFlow:reason": ("g:rmskip",),
    "AdminFlow:add_service": ("a:svc:",),
    "AdminFlow:edit_price": ("a:svc:",),
}


class TextStateGuard(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None and isinstance(event, CallbackQuery):
            allowed = TEXT_STATES.get(await state.get_state() or "")
            if allowed is not None and not (event.data or "").startswith(allowed):
                await state.clear()
        return await handler(event, data)
