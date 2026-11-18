"""Подмены для тестов: Supabase (таблица groups в памяти), Telegram (без сети) и диалог."""

import asyncio
import copy
import datetime
import json
import uuid
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update
from aiogram.types import User as TgUser

import create
import handlers
import join
from db import Database


def gid(n: int) -> str:
    return f"00000000-0000-4000-8000-00000000000{n}"


G1, G2, G3, G4, G5 = (gid(n) for n in range(1, 6))
OWNER1, OWNER2 = 100, 101  # tg_id организаторов в тестовых группах


def owner_entry(tg_id: int, username: str) -> dict:
    return {
        "tg_id": tg_id,
        "tg_username": f"@{username}",
        "name": username,
        "role": "owner",
        "status": "active",
    }


def member_entry(tg_id: int, username: str, email: str) -> dict:
    return {
        "tg_id": tg_id,
        "tg_username": f"@{username}",
        "name": username,
        "email": email,
        "role": "member",
        "status": "active",
    }


def make_group(
    group_id: str, slug: str, max_slots: int, current: int, status: str, **extra
) -> dict:
    return {
        "id": group_id,
        "service_slug": slug,
        "creator_tg_username": "@owner1",
        "service_name": slug,
        "creator_email": "not_required",
        "price_per_slot": 850,
        "max_slots": max_slots,
        "total_slots": max_slots,
        "current_slots": current,
        "occupied_slots": current,
        "billing_date": "2026-12-05",
        "invite_link": None,
        "members": [owner_entry(OWNER1, "owner1")],
        "status": status,
        **extra,
    }


by_owner3 = {"creator_tg_username": "@owner3", "members": [owner_entry(102, "owner3")]}
GROUPS = [
    make_group(G1, "yandex_plus", 4, 1, "open"),
    make_group(
        G2,
        "yandex_plus",
        4,
        3,
        "open",
        creator_tg_username="@owner2",
        billing_date="2026-12-10",
        invite_link="https://ya.plus/invite/abc",
        members=[
            owner_entry(OWNER2, "owner2"),
            member_entry(201, "anna", "anna@mail.kz"),
            member_entry(202, "boris", "boris@mail.kz"),
        ],
    ),
    make_group(G3, "yandex_plus", 4, 4, "full", **by_owner3),
    make_group(G4, "chatgpt_team", 4, 2, "open", price_per_slot=6250, **by_owner3),
    make_group(G5, "youtube_premium", 6, 1, "open", price_per_slot=900, **by_owner3),
]


class FakeTable:
    """Минимальная имитация таблицы Supabase: select/insert/update, eq и filter(cs)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows  # тот же список, что в клиенте: изменения сохраняются
        self.filters: list = []
        self.mode, self.values = "select", {}

    def select(self, _: str):
        return self

    def insert(self, values: dict):
        self.mode, self.values = "insert", values
        return self

    def update(self, values: dict):
        self.mode, self.values = "update", values
        return self

    def eq(self, column: str, value: object):
        self.filters.append(lambda r: r.get(column) == value)
        return self

    def filter(self, column: str, op: str, value: str):
        assert op == "cs", op
        wanted = json.loads(value)
        self.filters.append(
            lambda r: all(
                any(all(item.get(k) == v for k, v in w.items()) for item in (r.get(column) or []))
                for w in wanted
            )
        )
        return self

    async def execute(self):
        if self.mode == "insert":
            row = {"id": str(uuid.uuid4()), **copy.deepcopy(self.values)}
            self.rows.append(row)
            return SimpleNamespace(data=[copy.deepcopy(row)])
        found = [r for r in self.rows if all(f(r) for f in self.filters)]
        if self.mode == "update":
            for row in found:
                row.update(copy.deepcopy(self.values))
        return SimpleNamespace(data=[copy.deepcopy(r) for r in found])


class FakeClient:
    def __init__(self) -> None:
        self.tables = {"groups": copy.deepcopy(GROUPS)}

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


def message_update(text: str, username: str | None = None, user_id: int = 7) -> Update:
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


def callback_update(data: str, username: str | None = None, user_id: int = 7) -> Update:
    user = TgUser(id=user_id, is_bot=False, first_name="Али", username=username)
    msg = Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=Chat(id=user_id, type="private"),
        text="old",
    )
    cb = CallbackQuery(id="1", from_user=user, chat_instance="c", message=msg, data=data)
    return Update(update_id=2, callback_query=cb)


def buttons(call) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in call.reply_markup.inline_keyboard for b in row]


class Chat_:
    """Один пользователь в одном диалоге: шлёт сообщения, нажимает кнопки, читает ответы."""

    def __init__(self, username: str | None = "bob", user_id: int = 7) -> None:
        self.session = FakeSession()
        for module in (handlers, create, join):
            module.router._parent_router = None  # роутеры модульные: отцепляем от прошлого теста
        self.dp = Dispatcher()
        self.dp.include_router(handlers.router)
        self.dp.include_router(create.router)
        self.dp.include_router(join.router)
        self.bot = Bot("1:TEST", session=self.session)
        self.db = Database(FakeClient())  # type: ignore[arg-type]
        self.username, self.user_id = username, user_id

    def feed(self, update: Update) -> None:
        asyncio.run(self.dp.feed_update(self.bot, update, db=self.db))

    def say(self, text: str) -> None:
        self.feed(message_update(text, self.username, self.user_id))

    def press(self, data: str) -> None:
        self.feed(callback_update(data, self.username, self.user_id))

    def sent(self, chat_id: int | None = None) -> list:
        calls = [c for c in self.session.calls if isinstance(c, SendMessage | EditMessageText)]
        return [c for c in calls if chat_id is None or c.chat_id == chat_id]

    def last(self):
        return self.sent(self.user_id)[-1]

    def alerts(self) -> list[str]:
        return [
            c.text
            for c in self.session.calls
            if type(c).__name__ == "AnswerCallbackQuery" and c.text
        ]

    @property
    def groups(self) -> list[dict]:
        return self.db.client.tables["groups"]  # type: ignore[attr-defined]

    def group(self, group_id: str) -> dict:
        return next(g for g in self.groups if g["id"] == group_id)

    def _key(self) -> StorageKey:
        return StorageKey(bot_id=self.bot.id, chat_id=self.user_id, user_id=self.user_id)

    def state(self) -> str | None:
        return asyncio.run(self.dp.storage.get_state(self._key()))

    def data(self) -> dict:
        return asyncio.run(self.dp.storage.get_data(self._key()))
