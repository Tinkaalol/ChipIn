"""«Я оплатил» → заявка админу → подтверждение или отказ → таблица groups."""

import pytest
from aiogram.methods import EditMessageText, SendMessage

from payments import APP_NO_PREFIX, APP_OK_PREFIX, USER_ACCEPTED
from tests.test_bot import G1, G2, buttons, callback_update, gid
from tests.test_flow import ADMIN, Chat


def apps(chat: Chat) -> list[dict]:
    return chat.db.client.tables["applications"]  # type: ignore[attr-defined]


def groups(chat: Chat) -> list[dict]:
    return chat.db.client.tables["groups"]  # type: ignore[attr-defined]


def group(chat: Chat, group_id: str) -> dict:
    return next(g for g in groups(chat) if g["id"] == group_id)


def to_admin(chat: Chat) -> list[SendMessage]:
    return [c for c in chat.session.calls if isinstance(c, SendMessage) and c.chat_id == ADMIN]


def to_user(chat: Chat) -> list[str]:
    return [c.text for c in chat.session.calls if isinstance(c, SendMessage) and c.chat_id == 7]


def submit(chat: Chat, start: str, email: str = "bob@mail.kz") -> None:
    chat.say(start)
    chat.say(email)
    chat.press("paid")


def admin_press(chat: Chat, prefix: str, message: SendMessage | None = None, who: int = ADMIN):
    app_id = apps(chat)[-1]["id"]
    text = (message or to_admin(chat)[-1]).text
    chat.feed(callback_update(f"{prefix}{app_id}", "boss", user_id=who, message_text=text))


def admin_edits(chat: Chat) -> list[EditMessageText]:
    return [c for c in chat.session.calls if isinstance(c, EditMessageText) and c.chat_id == ADMIN]


# --- «Я оплатил» ---


def test_paid_sends_user_confirmation_and_admin_details_with_two_buttons() -> None:
    chat = Chat("bob")
    submit(chat, "/start create_netflix")
    assert USER_ACCEPTED in to_user(chat)
    (message,) = to_admin(chat)
    for part in ("Создание новой группы", "Netflix", "2 500 ₸", "@bob", "ID 7", "bob@mail.kz"):
        assert part in message.text
    assert [text for text, _ in buttons(message)] == ["✅ Подтвердить оплату", "❌ Отклонить"]
    (app,) = apps(chat)
    assert app["status"] == "pending" and app["action"] == "create" and app["amount_kzt"] == 2500
    assert buttons(message)[0][1] == f"{APP_OK_PREFIX}{app['id']}"
    assert chat.state() is None  # диалог закончен


def test_join_request_text_for_admin() -> None:
    chat = Chat("bob")
    submit(chat, f"/start join_{G1}")
    text = to_admin(chat)[0].text
    assert "Присоединение к группе" in text and "занято 1 из 4" in text and G1 in text
    assert apps(chat)[0]["group_id"] == G1


def test_user_without_username_is_described_by_name_and_id() -> None:
    chat = Chat(username=None)
    submit(chat, "/start create_netflix")
    text = to_admin(chat)[0].text
    assert "без username" in text and "ID 7" in text and "Али" in text
    assert apps(chat)[0]["tg_username"] is None


def test_double_click_creates_one_application() -> None:
    chat = Chat()
    chat.say("/start create_netflix")
    chat.say("bob@mail.kz")
    chat.press("paid")
    chat.press("paid")
    assert len(apps(chat)) == 1 and len(to_admin(chat)) == 1


def test_admin_not_configured_or_unreachable_keeps_the_dialog() -> None:
    chat = Chat(admin_id=0)
    chat.say("/start create_netflix")
    chat.say("bob@mail.kz")
    chat.press("paid")
    assert "временно недоступен" in chat.alerts()[-1] and apps(chat) == []
    assert chat.state() is not None

    blocked = Chat()
    blocked.session.fail_chats.add(ADMIN)  # админ не запускал бота
    blocked.say("/start create_netflix")
    blocked.say("bob@mail.kz")
    blocked.press("paid")
    assert "Не получилось" in blocked.alerts()[-1]
    assert [a["status"] for a in apps(blocked)] == ["failed"]
    assert blocked.state() is not None and USER_ACCEPTED not in to_user(blocked)


# --- подтверждение: создание группы ---


def test_approve_create_inserts_group_and_notifies() -> None:
    chat = Chat("bob")
    submit(chat, "/start create_netflix")
    before = len(groups(chat))
    admin_press(chat, APP_OK_PREFIX)
    assert len(groups(chat)) == before + 1
    new = groups(chat)[-1]
    assert new["service_slug"] == "netflix" and new["creator_tg_username"] == "@bob"
    assert new["creator_email"] == "bob@mail.kz"
    assert new["max_slots"] == 4 and new["current_slots"] == 1 and new["status"] == "collecting"
    assert "Оплата подтверждена! Вы в группе. Ожидайте приглашение на bob@mail.kz." in to_user(chat)
    edit = admin_edits(chat)[-1]
    assert "<b>Подтверждено ✅</b>" in edit.text and edit.reply_markup is None
    assert apps(chat)[0]["status"] == "approved"


def test_approve_create_without_username_stores_tg_id() -> None:
    chat = Chat(username=None)
    submit(chat, "/start create_netflix")
    admin_press(chat, APP_OK_PREFIX)
    assert groups(chat)[-1]["creator_tg_username"] == "tg_id:7"


# --- подтверждение: вступление ---


def test_approve_join_increments_slots() -> None:
    chat = Chat()
    submit(chat, f"/start join_{G1}")
    admin_press(chat, APP_OK_PREFIX)
    assert group(chat, G1)["current_slots"] == 2 and group(chat, G1)["status"] == "collecting"
    assert any("Оплата подтверждена" in t for t in to_user(chat))


def test_last_place_marks_group_full() -> None:
    chat = Chat()
    submit(chat, f"/start join_{G2}")  # было 3 из 4
    admin_press(chat, APP_OK_PREFIX)
    assert group(chat, G2)["current_slots"] == 4 and group(chat, G2)["status"] == "full"


def test_group_filled_meanwhile_is_not_overfilled_and_application_stays_pending() -> None:
    chat = Chat()
    submit(chat, f"/start join_{G2}")
    group(chat, G2).update(current_slots=4, status="full")  # кто-то успел раньше
    admin_press(chat, APP_OK_PREFIX)
    assert group(chat, G2)["current_slots"] == 4
    assert apps(chat)[0]["status"] == "pending"
    assert "нет мест" in chat.alerts()[-1]
    assert not any("Оплата подтверждена" in t for t in to_user(chat))
    assert admin_edits(chat) == []  # кнопки остаются: можно отклонить и вернуть деньги
    admin_press(chat, APP_NO_PREFIX)
    assert apps(chat)[0]["status"] == "rejected"


def test_second_click_does_not_apply_twice() -> None:
    chat = Chat()
    submit(chat, f"/start join_{G1}")
    message = to_admin(chat)[0]
    admin_press(chat, APP_OK_PREFIX, message)
    admin_press(chat, APP_OK_PREFIX, message)  # устаревшая кнопка
    assert group(chat, G1)["current_slots"] == 2
    assert "уже обработана" in chat.alerts()[-1]
    admin_press(chat, APP_NO_PREFIX, message)  # и отклонить после подтверждения нельзя
    assert apps(chat)[0]["status"] == "approved"


def test_failure_while_updating_groups_reverts_the_claim() -> None:
    chat = Chat()
    submit(chat, "/start create_netflix")
    chat.db.client.tables["available_services"].clear()  # type: ignore[attr-defined]
    admin_press(chat, APP_OK_PREFIX)
    assert apps(chat)[0]["status"] == "pending" and "Ошибка" in chat.alerts()[-1]


def test_user_blocked_the_bot_admin_is_warned() -> None:
    chat = Chat()
    submit(chat, "/start create_netflix")
    chat.session.fail_chats.add(7)
    admin_press(chat, APP_OK_PREFIX)
    assert "Не удалось написать пользователю" in admin_edits(chat)[-1].text
    assert apps(chat)[0]["status"] == "approved"  # группа уже обновлена


# --- отказ ---


def test_reject_notifies_user_with_contact_and_marks_message() -> None:
    chat = Chat()
    submit(chat, f"/start join_{G1}")
    before = [dict(g) for g in groups(chat)]
    admin_press(chat, APP_NO_PREFIX)
    assert "не нашли ваш перевод" in to_user(chat)[-1] and "@chipin_help" in to_user(chat)[-1]
    edit = admin_edits(chat)[-1]
    assert "<b>Отклонено ❌</b>" in edit.text and edit.reply_markup is None
    assert apps(chat)[0]["status"] == "rejected" and groups(chat) == before


def test_reject_without_support_contact_links_to_admin() -> None:
    chat = Chat(support="")
    submit(chat, "/start create_netflix")
    admin_press(chat, APP_NO_PREFIX)
    assert f"tg://user?id={ADMIN}" in to_user(chat)[-1]


# --- доступ ---


@pytest.mark.parametrize("prefix", [APP_OK_PREFIX, APP_NO_PREFIX])
def test_only_admin_can_decide(prefix: str) -> None:
    chat = Chat()
    submit(chat, f"/start join_{G1}")
    admin_press(chat, prefix, who=7)  # сам заявитель пытается подтвердить
    assert "Нет доступа" in chat.alerts()[-1]
    assert apps(chat)[0]["status"] == "pending" and group(chat, G1)["current_slots"] == 1


def test_admin_id_zero_never_matches_anyone() -> None:
    chat = Chat(admin_id=0)
    chat.db.client.tables["applications"].append(  # type: ignore[attr-defined]
        {
            "id": gid(7),
            "action": "join",
            "service_slug": "netflix",
            "group_id": G1,
            "email": "x@y.kz",
            "tg_username": None,
            "tg_id": 1,
            "tg_name": "x",
            "amount_kzt": 2500,
            "status": "pending",
        }
    )
    chat.feed(callback_update(f"{APP_OK_PREFIX}{gid(7)}", user_id=0))
    assert chat.db.client.tables["applications"][0]["status"] == "pending"  # type: ignore[attr-defined]


def test_garbage_application_id_is_harmless() -> None:
    chat = Chat()
    chat.feed(callback_update(f"{APP_OK_PREFIX}not-a-uuid", user_id=ADMIN))
    assert "уже обработана или не найдена" in chat.alerts()[-1]
