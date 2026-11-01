import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User


class FakeSession(BaseSession):
    """Сессия бота без сети: запоминает отправленные методы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            return Message(
                message_id=1,
                date=datetime.datetime.now(),
                chat=Chat(id=5, type="private"),
                text=method.text,
            ).as_(bot)
        return True

    def texts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, SendMessage | EditMessageText)]

    def alerts(self) -> list[str]:
        return [
            getattr(m, "text", "") or ""
            for m in self.calls
            if type(m).__name__ == "AnswerCallbackQuery"
        ]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot("1:TEST", session=session), session


_counter = [0]


def message_update(user_id: int, text: str, username: str = "u") -> Update:
    _counter[0] += 1
    entities = None
    if text.startswith("/"):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    message = Message(
        message_id=_counter[0],
        date=datetime.datetime.now(),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="N", username=username),
        text=text,
        entities=entities,
    )
    return Update(update_id=_counter[0], message=message)


def callback_update(user_id: int, data: str, username: str = "u") -> Update:
    _counter[0] += 1
    message = Message(
        message_id=_counter[0],
        date=datetime.datetime.now(),
        chat=Chat(id=user_id, type="private"),
        text="x",
    )
    callback = CallbackQuery(
        id=str(_counter[0]),
        from_user=User(id=user_id, is_bot=False, first_name="N", username=username),
        chat_instance="c",
        message=message,
        data=data,
    )
    return Update(update_id=_counter[0], callback_query=callback)
