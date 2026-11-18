from aiogram.methods import SendMessage

from tests.fakes import G1, G2, G3, G4, OWNER1, Chat_, buttons, gid


def sent_to(chat: Chat_, chat_id: int) -> list[str]:
    return [
        c.text for c in chat.session.calls if isinstance(c, SendMessage) and c.chat_id == chat_id
    ]


def test_deep_link_asks_email_and_warns_about_passwords() -> None:
    chat = Chat_()
    chat.say(f"/start join_{G1}")
    text = chat.last().text
    assert "Яндекс Плюс" in text and "850 ₸" in text and "личный email" in text
    assert "Чужие пароли" in text and chat.state() == "JoinGroup:email"
    assert chat.data()["group_id"] == G1


def test_button_from_the_list_starts_the_same_flow() -> None:
    chat = Chat_()
    chat.press(f"join_{G2}")
    assert chat.state() == "JoinGroup:email" and chat.data()["group_id"] == G2


def test_email_validation_keeps_the_step() -> None:
    chat = Chat_()
    chat.say(f"/start join_{G1}")
    for bad in ("hello", "bob@mail", "a b@c.d", "@x.kz", "bob@@x.kz"):
        chat.say(bad)
        assert "не похоже на email" in chat.last().text, bad
    assert chat.state() == "JoinGroup:email" and len(chat.group(G1)["members"]) == 1


def test_join_adds_active_member_and_takes_a_slot() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G1}")
    chat.say("Bob@Mail.kz")
    group = chat.group(G1)
    assert group["current_slots"] == group["occupied_slots"] == 2 and group["status"] == "open"
    assert len(group["members"]) == 2
    member = group["members"][-1]
    assert member["tg_id"] == 7 and member["tg_username"] == "@bob"
    assert member["email"] == "bob@mail.kz" and member["status"] == "active"
    assert member["role"] == "member" and member["joined_at"]
    assert group["members"][0]["role"] == "owner"  # организатор на месте
    text = chat.last().text
    assert "Вы в группе" in text and "850 ₸" in text and "05.12.2026" in text
    assert "Приглашение придёт на bob@mail.kz" in text and "@owner1" in text
    assert chat.state() is None


def test_invite_link_is_given_when_the_group_has_one() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G2}")
    chat.say("bob@mail.kz")
    text = chat.last().text
    assert "https://ya.plus/invite/abc" in text and "10.12.2026" in text
    assert "Приглашение придёт" not in text


def test_last_free_place_marks_group_full_and_removes_it_from_lists() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G2}")  # было 3 из 4
    chat.say("bob@mail.kz")
    assert chat.group(G2)["current_slots"] == 4 and chat.group(G2)["status"] == "full"
    chat.press("svc_yandex_plus")
    assert f"join_{G2}" not in [data for _, data in buttons(chat.last())]


def test_organizer_gets_the_new_members_email() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G1}")
    chat.say("bob@mail.kz")
    (note,) = sent_to(chat, OWNER1)
    assert "Новый участник" in note and "@bob" in note and "bob@mail.kz" in note
    assert "Свободных мест: 2 из 4" in note


def test_blocked_organizer_does_not_break_joining() -> None:
    chat = Chat_("bob")
    chat.session.fail_chats.add(OWNER1)
    chat.say(f"/start join_{G1}")
    chat.say("bob@mail.kz")
    assert chat.group(G1)["current_slots"] == 2 and "Вы в группе" in chat.last().text


def test_group_created_outside_the_bot_has_nobody_to_notify() -> None:
    chat = Chat_("bob")
    chat.group(G1)["members"] = None
    chat.say(f"/start join_{G1}")
    chat.say("bob@mail.kz")
    assert "Вы в группе" in chat.last().text and sent_to(chat, OWNER1) == []


def test_no_places_apologises_and_offers_new_group() -> None:
    for group in (G3, G2):
        chat = Chat_("bob")
        if group == G2:
            chat.group(G2).update(current_slots=4)
        chat.say(f"/start join_{group}")
        reply = chat.last()
        assert "нет мест" in reply.text and chat.state() is None
        assert ("➕ Создать новую группу", "create_yandex_plus") in buttons(reply)


def test_unknown_group() -> None:
    for group_id in (gid(9), "not-a-uuid", "x" * 40):
        chat = Chat_()
        chat.say(f"/start join_{group_id}")
        assert "не нашлось" in chat.last().text and chat.state() is None


def test_cannot_join_own_group_or_twice() -> None:
    owner = Chat_("owner1", OWNER1)
    owner.say(f"/start join_{G1}")
    assert "ваша группа" in owner.last().text and owner.state() is None
    member = Chat_("anna", 201)
    member.say(f"/start join_{G2}")
    assert "уже участник" in member.last().text and member.state() is None
    assert len(member.group(G2)["members"]) == 3


def test_group_filled_while_typing_email() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G2}")
    chat.group(G2).update(current_slots=4, status="full")  # кто-то успел раньше
    chat.say("bob@mail.kz")
    assert "места закончились" in chat.last().text and chat.state() is None
    assert len(chat.group(G2)["members"]) == 3 and chat.group(G2)["current_slots"] == 4


def test_second_email_message_does_not_add_twice() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G1}")
    chat.say("bob@mail.kz")
    chat.say("bob@mail.kz")  # диалог уже завершён
    assert len(chat.group(G1)["members"]) == 2 and chat.group(G1)["current_slots"] == 2


def test_join_then_my_subs_and_cancel() -> None:
    chat = Chat_("bob")
    chat.say(f"/start join_{G4}")
    chat.press("cancel_join")
    assert chat.state() is None and len(chat.group(G4)["members"]) == 1
    chat.say(f"/start join_{G4}")
    chat.say("bob@mail.kz")
    chat.say("/my_subs")
    text = chat.last().text
    assert "ChatGPT Team" in text and "вы участник" in text and "6 250 ₸" in text
