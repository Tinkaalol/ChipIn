from handlers import ERROR_TEXT
from tests.fakes import G1, G2, G4, OWNER1, OWNER2, Chat_, buttons


def test_start_shows_fixed_catalog_and_actions() -> None:
    chat = Chat_()
    chat.say("/start")
    call = chat.last()
    assert "ChipIn" in call.text and "70%" in call.text
    assert buttons(call) == [
        ("Яндекс Плюс · 850 ₸/слот", "svc_yandex_plus"),
        ("YouTube Premium · 900 ₸/слот", "svc_youtube_premium"),
        ("ChatGPT Team · 6 250 ₸/слот", "svc_chatgpt_team"),
        ("Google One (2 ТБ) · 800 ₸/слот", "svc_google_one"),
        ("📂 Открытые группы", "open_groups"),
        ("➕ Создать группу", "newgroup"),
        ("📋 Мои подписки", "my_subs"),
    ]


def test_service_screen_shows_share_create_button_and_joinable_groups() -> None:
    chat = Chat_()
    chat.press("svc_yandex_plus")
    call = chat.last()
    assert "3 400 ₸" in call.text and "850 ₸" in call.text
    assert buttons(call) == [
        ("➕ Создать новую группу", "create_yandex_plus"),
        ("Яндекс Плюс · свободно 3 из 4", f"join_{G1}"),
        ("Яндекс Плюс · свободно 1 из 4", f"join_{G2}"),
        ("‹ В меню", "services"),
    ]  # полная группа и чужие сервисы не показаны


def test_service_without_groups_and_unknown_service() -> None:
    chat = Chat_()
    chat.groups.clear()
    chat.press("svc_google_one")
    assert "Открытых групп пока нет" in chat.last().text
    chat.press("svc_nope")
    assert "недоступен" in chat.alerts()[-1]


def test_open_groups_across_services() -> None:
    chat = Chat_()
    chat.press("open_groups")
    labels = [text for text, _ in buttons(chat.last())]
    assert labels == [
        "YouTube Premium · свободно 5 из 6",
        "Яндекс Плюс · свободно 3 из 4",
        "ChatGPT Team · свободно 2 из 4",
        "Яндекс Плюс · свободно 1 из 4",
        "‹ В меню",
    ]


def test_new_group_menu_lists_services_to_create() -> None:
    chat = Chat_()
    chat.press("newgroup")
    got = [data for _, data in buttons(chat.last())]
    assert got == [
        "create_yandex_plus",
        "create_youtube_premium",
        "create_chatgpt_team",
        "create_google_one",
        "services",
    ]


# --- мои подписки ---


def test_my_subs_for_organizer() -> None:
    chat = Chat_(username="owner1", user_id=OWNER1)
    chat.say("/my_subs")
    text = chat.last().text
    assert "Яндекс Плюс" in text and "вы организатор" in text
    assert "850 ₸" in text and "05.12.2026" in text  # доля и ближайшее списание
    assert "занято 1 из 4" in text and "пока никого" in text
    assert "YouTube" not in text  # чужие группы не показываются


def test_my_subs_for_member_shows_share_date_and_organizer() -> None:
    chat = Chat_(username="anna", user_id=201)
    chat.press("my_subs")
    text = chat.last().text
    assert "вы участник" in text and "850 ₸" in text and "10.12.2026" in text
    assert "Организатор: @owner2" in text
    assert "boris" not in text and "anna@mail.kz" not in text  # чужие данные не раскрываем


def test_my_subs_organizer_sees_members_and_invite_link() -> None:
    chat = Chat_(username="owner2", user_id=OWNER2)
    chat.say("/my_subs")
    text = chat.last().text
    assert "@anna" in text and "@boris" in text and "https://ya.plus/invite/abc" in text
    assert "anna@mail.kz" not in text  # email есть только в уведомлении о вступлении


def test_my_subs_next_charge_rolls_forward_and_user_in_several_groups() -> None:
    chat = Chat_(username="owner1", user_id=OWNER1)
    chat.group(G1)["billing_date"] = "2026-09-15"  # дата в прошлом: списание ежемесячное
    chat.group(G4)["members"].append(
        {"tg_id": OWNER1, "tg_username": "@owner1", "role": "member", "status": "active"}
    )
    chat.say("/my_subs")
    text = chat.last().text
    assert "15.12.2026" in text and "вы участник" in text and "ChatGPT Team" in text
    assert "6 250 ₸" in text


def test_my_subs_found_by_username_for_groups_without_owner_entry() -> None:
    chat = Chat_(username="legacy", user_id=555)
    chat.groups.append(
        {
            "id": "00000000-0000-4000-8000-0000000000aa",
            "service_slug": "google_one",
            "creator_tg_username": "@legacy",
            "max_slots": 6,
            "current_slots": 2,
            "billing_date": None,
            "invite_link": None,
            "members": None,
            "status": "open",
        }
    )
    chat.say("/my_subs")
    assert "Google One" in chat.last().text and "вы организатор" in chat.last().text


def test_my_subs_empty() -> None:
    chat = Chat_(username="nobody", user_id=999)
    chat.say("/my_subs")
    assert "пока нет подписок" in chat.last().text


def test_menu_buttons_reset_unfinished_dialog() -> None:
    chat = Chat_()
    chat.press("create_yandex_plus")
    assert chat.state() is not None
    chat.press("my_subs")
    assert chat.state() is None


def test_supabase_failure_still_answers_the_user() -> None:
    chat = Chat_()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("Supabase недоступен")

    chat.db.list_open_groups = broken  # type: ignore[method-assign]
    chat.press("open_groups")
    assert ERROR_TEXT in chat.alerts()
