import asyncio
import copy
import datetime
import uuid
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

import admin
import flow
import handlers
from db import Database, free_slots


def gid(n: int) -> str:
    return f"00000000-0000-4000-8000-00000000000{n}"


G1, G2, G3, G4, G5 = (gid(n) for n in range(1, 6))
SERVICES = [
    {"slug": "netflix", "title": "Netflix", "price_per_person_kzt": 2500, "total_slots": 4},
    {
        "slug": "spotify",
        "title": "Spotify <Family>",
        "price_per_person_kzt": 1200,
        "total_slots": 6,
    },
]
GROUPS = [
    {
        "id": G1,
        "creator_tg_username": "owner1",
        "service_slug": "netflix",
        "max_slots": 4,
        "current_slots": 1,
        "status": "collecting",
    },
    {
        "id": G2,
        "service_slug": "netflix",
        "max_slots": 4,
        "current_slots": 3,
        "status": "collecting",
    },
    {
        "id": G3,
        "service_slug": "netflix",
        "max_slots": 4,
        "current_slots": 4,
        "status": "collecting",
    },
    {"id": G4, "service_slug": "netflix", "max_slots": 4, "current_slots": 2, "status": "full"},
    {
        "id": G5,
        "service_slug": "spotify",
        "max_slots": 6,
        "current_slots": 2,
        "status": "collecting",
    },
]


class FakeTable:
    """Минимальная имитация таблицы Supabase: select/insert/update с фильтрами eq."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows  # тот же список, что в клиенте: изменения сохраняются
        self.filters: list[tuple[str, object]] = []
        self.mode, self.values = "select", {}
        self.sort: str | None = None

    def select(self, _: str):
        return self

    def insert(self, values: dict):
        self.mode, self.values = "insert", values
        return self

    def update(self, values: dict):
        self.mode, self.values = "update", values
        return self

    def eq(self, column: str, value: object):
        self.filters.append((column, value))
        return self

    def order(self, column: str):
        self.sort = column
        return self

    async def execute(self):
        if self.mode == "insert":
            row = {"id": str(uuid.uuid4()), **self.values}
            self.rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        found = [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]
        if self.mode == "update":
            for row in found:
                row.update(self.values)
        if self.sort:
            found = sorted(found, key=lambda r: r[self.sort])
        return SimpleNamespace(data=[dict(r) for r in found])


class FakeClient:
    def __init__(self) -> None:
        self.tables = copy.deepcopy(
            {"available_services": SERVICES, "groups": GROUPS, "applications": []}
        )

    def table(self, name: str) -> FakeTable:
        return FakeTable(self.tables[name])


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []
        self.fail_chats: set[int] = set()  # куда бот «не может» писать (заблокирован)

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage) and method.chat_id in self.fail_chats:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        return Message(
            message_id=1, date=datetime.datetime.now(), chat=Chat(id=5, type="private"), text="x"
        ).as_(bot)


def make_env() -> tuple[Dispatcher, Bot, FakeSession, Database]:
    session = FakeSession()
    handlers.router._parent_router = None  # роутер модульный: отцепляем от прошлого теста
    dp = Dispatcher()
    flow.router._parent_router = None
    admin.router._parent_router = None
    dp.include_router(handlers.router)
    dp.include_router(flow.router)
    dp.include_router(admin.router)
    return dp, Bot("1:TEST", session=session), session, Database(FakeClient())  # type: ignore[arg-type]


def buttons(call) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in call.reply_markup.inline_keyboard for b in row]


def message_update(text: str, username: str | None = None, user_id: int = 7) -> Update:
    from aiogram.types import MessageEntity

    user = TgUser(id=user_id, is_bot=False, first_name="Али", username=username)
    entities = None
    if text.startswith("/"):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    msg = Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text=text,
        entities=entities,
    )
    return Update(update_id=1, message=msg)


def callback_update(
    data: str, username: str | None = None, user_id: int = 7, message_text: str = "old"
) -> Update:
    from aiogram.types import CallbackQuery

    user = TgUser(id=user_id, is_bot=False, first_name="Али", username=username)
    msg = Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=Chat(id=user_id, type="private"),
        text=message_text,
    )
    cb = CallbackQuery(id="1", from_user=user, chat_instance="c", message=msg, data=data)
    return Update(update_id=2, callback_query=cb)


def run(coro):
    return asyncio.run(coro)


def test_start_shows_welcome_services_and_open_groups_button() -> None:
    dp, bot, session, db = make_env()
    run(dp.feed_update(bot, message_update("/start"), db=db))
    (call,) = [c for c in session.calls if isinstance(c, SendMessage)]
    assert "ChipIn" in call.text and "70%" in call.text
    assert buttons(call) == [
        ("Netflix · 2 500 ₸/мес", "svc_netflix"),
        ("Spotify <Family> · 1 200 ₸/мес", "svc_spotify"),
        ("📂 Открытые группы", "open_groups"),
    ]


def test_start_with_parameter_is_not_handled_here() -> None:
    dp, bot, session, db = make_env()
    run(dp.feed_update(bot, message_update("/start ref_1"), db=db))
    assert session.calls == []


def test_service_screen_has_create_button_and_joinable_groups_only() -> None:
    dp, bot, session, db = make_env()
    run(dp.feed_update(bot, callback_update("svc_netflix"), db=db))
    (call,) = [c for c in session.calls if isinstance(c, EditMessageText)]
    assert "Netflix" in call.text and "2 500 ₸" in call.text
    assert buttons(call) == [
        ("➕ Создать новую группу", "create_netflix"),
        ("Netflix · свободно 3 из 4", f"join_{G1}"),  # больше мест выше
        ("Netflix · свободно 1 из 4", f"join_{G2}"),
        ("‹ К сервисам", "services"),
    ]  # полная (g-3), закрытая по статусу (g-4) и чужой сервис (g-5) не показаны


def test_service_without_groups_and_unknown_service() -> None:
    dp, bot, session, db = make_env()
    db.client.tables["groups"] = []  # type: ignore[attr-defined]
    run(dp.feed_update(bot, callback_update("svc_spotify"), db=db))
    (call,) = [c for c in session.calls if isinstance(c, EditMessageText)]
    assert "Открытых групп пока нет" in call.text and "&lt;Family&gt;" in call.text
    run(dp.feed_update(bot, callback_update("svc_nope"), db=db))
    answers = [c for c in session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert any(c.text and "недоступен" in c.text for c in answers)


def test_open_groups_across_services() -> None:
    dp, bot, session, db = make_env()
    run(dp.feed_update(bot, callback_update("open_groups"), db=db))
    (call,) = [c for c in session.calls if isinstance(c, EditMessageText)]
    labels = [text for text, _ in buttons(call)]
    assert labels == [  # больше свободных мест выше, независимо от сервиса
        "Spotify <Family> · свободно 4 из 6",
        "Netflix · свободно 3 из 4",
        "Netflix · свободно 1 из 4",
        "‹ К сервисам",
    ]


def test_free_slots() -> None:
    assert free_slots({"max_slots": 4, "current_slots": 1}) == 3
