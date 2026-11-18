from db import INSERT_COLUMNS
from tests.fakes import Chat_, buttons


def start_create(chat: Chat_, slug: str = "yandex_plus") -> None:
    chat.say("/start")
    chat.press("newgroup")
    chat.press(f"create_{slug}")


def test_create_asks_free_slots_up_to_total_minus_one() -> None:
    chat = Chat_()
    start_create(chat, "chatgpt_team")
    call = chat.last()
    assert "6 250 ₸" in call.text and "фиксированная" in call.text
    frees = [data for _, data in buttons(call) if data.startswith("free_")]
    assert frees == ["free_1", "free_2", "free_3"]  # 4 места, одно занимает организатор
    assert ("✖ Отмена", "cancel_flow") in buttons(call) and ("‹ Назад", "back_services") in buttons(
        call
    )
    start_create(chat, "youtube_premium")
    frees = [data for _, data in buttons(chat.last()) if data.startswith("free_")]
    assert len(frees) == 5


def test_full_flow_saves_group_with_fixed_price() -> None:
    chat = Chat_("bob")
    start_create(chat)
    chat.press("free_2")
    assert "ГГГГ-ММ-ДД" in chat.last().text
    chat.say("2026-12-05")
    assert "инвайт-ссылку" in chat.last().text
    chat.say("https://ya.plus/invite/xyz")
    row = chat.groups[-1]
    assert row["service_slug"] == "yandex_plus" and row["creator_tg_username"] == "@bob"
    assert row["price_per_slot"] == 850 and row["max_slots"] == 4 and row["total_slots"] == 4
    assert row["current_slots"] == row["occupied_slots"] == 2  # заняты организатор и ещё один
    assert row["service_name"] == "Яндекс Плюс" and row["creator_email"] == "not_required"
    assert set(row) - {"id"} == INSERT_COLUMNS  # заполнены все колонки, ни одной лишней
    assert (
        row["billing_date"] == "2026-12-05" and row["invite_link"] == "https://ya.plus/invite/xyz"
    )
    assert row["status"] == "open"
    (owner,) = row["members"]
    assert owner["tg_id"] == 7 and owner["role"] == "owner" and owner["status"] == "active"
    text = chat.last().text
    assert "Группа создана" in text and "850 ₸" in text and "2026-12-05" in text
    assert chat.state() is None


def test_later_by_word_and_by_button_and_user_without_username() -> None:
    chat = Chat_(username=None)
    start_create(chat, "google_one")
    chat.press("free_5")
    chat.say("2026-11-18")
    chat.say("Позже")
    row = chat.groups[-1]
    assert row["invite_link"] is None and row["current_slots"] == 1
    assert row["creator_tg_username"] == "tg_id:7" and row["price_per_slot"] == 800

    other = Chat_("bob")
    start_create(other)
    other.press("free_1")
    other.say("2026-12-05")
    other.press("link_later")
    assert other.groups[-1]["invite_link"] is None and other.groups[-1]["current_slots"] == 3


def test_double_tap_later_creates_one_group() -> None:
    chat = Chat_()
    start_create(chat)
    chat.press("free_1")
    chat.say("2026-12-05")
    before = len(chat.groups)
    chat.press("link_later")
    chat.press("link_later")
    assert len(chat.groups) == before + 1


def test_invalid_input_keeps_the_step() -> None:
    chat = Chat_()
    start_create(chat)
    chat.press("free_1")
    for bad in ("завтра", "05.12.2026", "2026-02-30", "2020-01-01", "2030-01-01"):
        chat.say(bad)
        assert chat.state() == "CreateGroup:date", bad
    assert "прошла" in [chat.last().text][0] or "далеко" in chat.last().text
    chat.say("2026-12-05")
    for bad in ("ya.ru", "http://", "my link https://x.kz", "ftp://x.kz/a"):
        chat.say(bad)
        assert chat.state() == "CreateGroup:link", bad
    assert "ссылку целиком" in chat.last().text
    assert len(chat.groups) == 5  # ничего не сохранено


def test_free_button_out_of_range_is_rejected() -> None:
    chat = Chat_()
    start_create(chat)
    chat.press("free_4")  # столько мест нет
    chat.press("free_0")
    chat.press("free_x")
    assert len(chat.alerts()) == 3 and chat.state() == "CreateGroup:free"


def test_deep_link_starts_at_free_slots_and_unknown_slug() -> None:
    chat = Chat_()
    chat.say("/start create_yandex_plus")
    assert "Сколько мест свободно" in chat.last().text and chat.state() == "CreateGroup:free"
    chat.say("/start create_unknown")
    assert "недоступен" in chat.last().text and chat.state() is None


def test_back_and_cancel() -> None:
    chat = Chat_()
    start_create(chat)
    chat.press("free_1")
    chat.press("back_free")
    assert "Сколько мест свободно" in chat.last().text
    chat.press("free_1")
    chat.say("2026-12-05")
    chat.press("back_date")
    assert "ГГГГ-ММ-ДД" in chat.last().text and chat.state() == "CreateGroup:date"
    chat.press("back_free")
    chat.press("back_services")
    assert "Для какого сервиса" in chat.last().text and chat.state() is None
    start_create(chat)
    chat.press("cancel_flow")
    assert chat.state() is None and chat.data() == {}


def test_plain_start_clears_dialog_and_text_without_dialog_is_ignored() -> None:
    chat = Chat_()
    start_create(chat)
    chat.say("/start")
    assert chat.state() is None
    count = len(chat.sent())
    chat.say("2026-12-05")  # диалога нет: бот не реагирует
    assert len(chat.sent()) == count
