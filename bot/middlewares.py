from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from bot.services.groups import SessionMaker
from bot.services.moderation import is_banned

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
