import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import EditMessageText, SendMessage

from flow import PAID_CB, Application, valid_email
from tests.test_bot import (
    G1,
    G2,
    G3,
    G4,
    buttons,
    callback_update,
    gid,
    make_env,
    message_update,
    run,
)

KASPI = "Kaspi Gold +7 700 000 00 00 (Иван И.)"


class Chat:
    """Один пользователь в одном диалоге: шлёт сообщения и нажимает кнопки."""

    def __init__(self, username: str | None = "bob") -> None:
        self.dp, self.bot, self.session, self.db = make_env()
        self.username = username

    def say(self, text: str) -> None:
        run(
            self.dp.feed_update(
                self.bot, message_update(text, self.username), db=self.db, kaspi_info=KASPI
            )
        )

    def press(self, data: str) -> None:
        run(
            self.dp.feed_update(
                self.bot, callback_update(data, self.username), db=self.db, kaspi_info=KASPI
            )
        )

    def texts(self) -> list[str]:
        return [c.text for c in self.session.calls if isinstance(c, SendMessage | EditMessageText)]

    def last_call(self):
        return [c for c in self.session.calls if isinstance(c, SendMessage | EditMessageText)][-1]

    def data(self) -> dict:
        key = StorageKey(bot_id=self.bot.id, chat_id=7, user_id=7)
        return run(self.dp.storage.get_data(key))

    def state(self) -> str | None:
        key = StorageKey(bot_id=self.bot.id, chat_id=7, user_id=7)
        return run(self.dp.storage.get_state(key))

    def alerts(self) -> list[str]:
        return [
            c.text
            for c in self.session.calls
            if type(c).__name__ == "AnswerCallbackQuery" and c.text
        ]


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("ali@example.com", True),
        ("  ali@mail.kz ", True),
        ("ali", False),
        ("ali@example", False),
        ("ali@.com", False),
        ("a li@example.com", False),
        ("@example.com", False),
        ("ali@@example.com", False),
        ("", False),
    ],
)
def test_email_validation(email: str, ok: bool) -> None:
    assert valid_email(email) is ok


def test_create_deep_link_starts_scenario_and_warns_about_passwords() -> None:
    chat = Chat()
    chat.say("/start create_netflix")
    prompt = chat.last_call()
    assert "создать новую группу" in prompt.text and "Netflix" in prompt.text
    assert "личный email" in prompt.text and "Чужие пароли" in prompt.text
    assert chat.state() == Application.email.state
    assert chat.data()["action"] == "create" and chat.data()["service_slug"] == "netflix"


def test_create_deep_link_unknown_service() -> None:
    chat = Chat()
    chat.say("/start create_nope")
    assert "недоступен" in chat.last_call().text and chat.state() is None


def test_full_create_flow_to_summary_card() -> None:
    chat = Chat("bob")
    chat.say("/start create_netflix")
    for bad in ("hello", "bob@mail", "a b@c.d"):
        chat.say(bad)
        assert "не похоже на email" in chat.last_call().text
    assert chat.state() == Application.email.state and "email" not in chat.data()
    chat.say("Bob@Mail.kz")
    card = chat.last_call()
    assert "Netflix" in card.text and "bob@mail.kz" in card.text
    assert "2 500 ₸" in card.text and KASPI in card.text
    assert (("✅ Я оплатил", PAID_CB)) in buttons(card)
    data = chat.data()
    assert data["action"] == "create" and data["service_slug"] == "netflix"
    assert data["email"] == "bob@mail.kz" and data["username"] == "@bob" and data["tg_id"] == 7
    assert chat.state() == Application.confirm.state


def test_user_without_username_is_stored_by_id_and_name() -> None:
    chat = Chat(username=None)
    chat.say("/start create_spotify")
    data = chat.data()
    assert data["username"] is None and data["tg_id"] == 7 and data["name"] == "Али"


def test_summary_escapes_html_in_email_and_shows_missing_requisites() -> None:
    chat = Chat()
    chat.say("/start create_netflix")
    run(
        chat.dp.feed_update(
            chat.bot, message_update("<b>x</b>@mail.kz", "bob"), db=chat.db, kaspi_info=""
        )
    )
    assert "реквизиты уточните" in chat.last_call().text
    assert "<b>x</b>" not in chat.last_call().text


def test_join_deep_link_with_free_places() -> None:
    chat = Chat()
    chat.say(f"/start join_{G1}")
    assert "вступить в группу" in chat.last_call().text
    data = chat.data()
    assert data["action"] == "join" and data["group_id"] == G1 and data["service_slug"] == "netflix"
    chat.say("bob@mail.kz")
    assert "Вступление в группу" in chat.last_call().text and "2 500 ₸" in chat.last_call().text


@pytest.mark.parametrize("group", [G3, G4])  # мест нет / набор закрыт
def test_join_deep_link_without_places_apologises_and_offers_new_group(group: str) -> None:
    chat = Chat()
    chat.say(f"/start join_{group}")
    reply = chat.last_call()
    assert "нет мест" in reply.text and chat.state() is None
    assert ("➕ Создать новую группу", "create_netflix") in buttons(reply)


@pytest.mark.parametrize("group_id", [gid(9), "not-a-uuid", "x" * 40])
def test_join_deep_link_unknown_group(group_id: str) -> None:
    chat = Chat()
    chat.say(f"/start join_{group_id}")
    assert "не нашлось" in chat.last_call().text and chat.state() is None


def test_cannot_join_own_group() -> None:
    chat = Chat("Owner1")
    chat.say(f"/start join_{G1}")
    assert "собственная группа" in chat.last_call().text and chat.state() is None


def test_buttons_start_the_same_scenarios() -> None:
    chat = Chat()
    chat.press("create_netflix")
    assert "создать новую группу" in chat.last_call().text
    chat.press(f"join_{G2}")
    assert "вступить в группу" in chat.last_call().text and chat.data()["group_id"] == G2


def test_new_link_in_the_middle_replaces_the_application() -> None:
    chat = Chat()
    chat.say("/start create_netflix")
    chat.say("/start create_spotify")
    assert chat.data()["service_slug"] == "spotify" and "email" not in chat.data()


def test_cancel_and_plain_start_clear_state() -> None:
    chat = Chat()
    chat.say("/start create_netflix")
    chat.press("cancel_flow")
    assert chat.state() is None and chat.data() == {}
    chat.say("/start create_netflix")
    chat.say("/start")
    assert chat.state() is None and "ChipIn" in chat.last_call().text


def test_non_text_message_in_email_step() -> None:
    import datetime

    from aiogram.types import Chat as TgChat
    from aiogram.types import Message, Update

    chat = Chat()
    chat.say("/start create_netflix")
    sticker = Message(
        message_id=2,
        date=datetime.datetime.now(),
        chat=TgChat(id=7, type="private"),
        from_user=message_update("x").message.from_user,
    )
    run(
        chat.dp.feed_update(
            chat.bot, Update(update_id=3, message=sticker), db=chat.db, kaspi_info=KASPI
        )
    )
    assert "обычным текстом" in chat.last_call().text and chat.state() == Application.email.state


def test_paid_button_only_in_confirm_state() -> None:
    chat = Chat()
    chat.press(PAID_CB)  # без заявки кнопка ничего не делает
    assert chat.alerts() == []
    chat.say("/start create_netflix")
    chat.say("bob@mail.kz")
    chat.press(PAID_CB)
    assert chat.alerts() and "в разработке" in chat.alerts()[-1]
