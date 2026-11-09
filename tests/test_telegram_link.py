import asyncio
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from backend.config import BackendSettings
from backend.main import create_app
from shared.db.models import TelegramLinkToken, User
from shared.services.telegram_link import (
    START_PREFIX,
    LinkError,
    create_link_token,
    hash_token,
    link_telegram,
    unlink_telegram,
)
from shared.services.users import chat_id_for, register_user
from tests.fakes import make_bot, message_update


async def web_user(sm, username: str | None = None) -> User:
    """Пользователь, пришедший на сайт без Telegram."""
    async with sm() as s:
        user = User(telegram_id=None, username=username)
        s.add(user)
        await s.commit()
        return user


async def test_successful_link_and_only_hash_is_stored(session_maker) -> None:
    user = await web_user(session_maker)
    token = await create_link_token(session_maker, user.id)
    result = await link_telegram(session_maker, token, 555, "ali")
    assert (result.user.telegram_id, result.user.telegram_username) == (555, "ali")
    async with session_maker() as s:
        saved = await s.get(User, user.id)
        row = await s.scalar(select(TelegramLinkToken))
    assert saved.telegram_id == 555 and saved.telegram_linked_at is not None
    assert saved.notify_enabled is True
    assert row.token_hash == hash_token(token) and token not in row.token_hash
    assert row.used_at is not None


async def test_token_lives_ten_minutes(session_maker) -> None:
    user = await web_user(session_maker)
    await create_link_token(session_maker, user.id)
    async with session_maker() as s:
        row = await s.scalar(select(TelegramLinkToken))
    ttl = row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(minutes=9, seconds=55) < ttl <= timedelta(minutes=10)


async def test_expired_token_is_rejected(session_maker) -> None:
    user = await web_user(session_maker)
    token = await create_link_token(session_maker, user.id)
    async with session_maker() as s:
        await s.execute(
            update(TelegramLinkToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    with pytest.raises(LinkError, match="истёк"):
        await link_telegram(session_maker, token, 555, "ali")
    async with session_maker() as s:
        assert (await s.get(User, user.id)).telegram_id is None


async def test_token_cannot_be_reused(session_maker) -> None:
    user = await web_user(session_maker)
    token = await create_link_token(session_maker, user.id)
    await link_telegram(session_maker, token, 555, "ali")
    await unlink_telegram(session_maker, user.id)
    with pytest.raises(LinkError, match="уже использована"):
        await link_telegram(session_maker, token, 777, "mallory")
    async with session_maker() as s:
        assert (await s.get(User, user.id)).telegram_id is None


async def test_unknown_token_is_rejected(session_maker) -> None:
    with pytest.raises(LinkError, match="недействительна"):
        await link_telegram(session_maker, "garbage", 1, None)


async def test_taken_telegram_id_is_refused_and_token_stays_unused(session_maker) -> None:
    occupant = await register_user(session_maker, 555, "owner_of_555")
    user = await web_user(session_maker)
    token = await create_link_token(session_maker, user.id)
    with pytest.raises(LinkError, match="уже привязан к другому"):
        await link_telegram(session_maker, token, 555, "ali")
    async with session_maker() as s:
        assert (await s.get(User, user.id)).telegram_id is None
        assert (await s.get(User, occupant.id)).telegram_id == 555
        assert (await s.scalar(select(TelegramLinkToken))).used_at is None


async def test_relinking_same_account_and_switching_account(session_maker) -> None:
    user = await register_user(session_maker, 555, "old")
    token = await create_link_token(session_maker, user.id)
    await link_telegram(session_maker, token, 888, "new")  # смена аккаунта Telegram
    async with session_maker() as s:
        saved = await s.get(User, user.id)
    assert (saved.telegram_id, saved.telegram_username) == (888, "new")


async def test_new_token_invalidates_previous_unused_one(session_maker) -> None:
    user = await web_user(session_maker)
    first = await create_link_token(session_maker, user.id)
    second = await create_link_token(session_maker, user.id)
    with pytest.raises(LinkError):
        await link_telegram(session_maker, first, 1, None)
    await link_telegram(session_maker, second, 1, None)


async def test_concurrent_use_links_once(file_session_maker) -> None:
    user = await web_user(file_session_maker)
    token = await create_link_token(file_session_maker, user.id)
    results = await asyncio.gather(
        *(link_telegram(file_session_maker, token, 100 + i, "x") for i in range(4)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1


async def test_unlink_clears_fields_and_notifications_need_a_chat(session_maker) -> None:
    user = await register_user(session_maker, 555, "ali")
    await unlink_telegram(session_maker, user.id)
    async with session_maker() as s:
        saved = await s.get(User, user.id)
    assert (saved.telegram_id, saved.telegram_username, saved.telegram_linked_at) == (None,) * 3
    assert chat_id_for(saved) is None
    saved.telegram_id, saved.notify_enabled = 5, False
    assert chat_id_for(saved) is None
    saved.notify_enabled = True
    assert chat_id_for(saved) == 5


# --- бот ---


async def start_with(dp, bot, telegram_id: int, token: str, username: str = "ali") -> None:
    await dp.feed_update(
        bot, message_update(telegram_id, f"/start {START_PREFIX}{token}", username)
    )


async def test_bot_links_account_and_confirms(dp, session_maker, monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://site.example")
    from shared.config import get_shared_settings

    get_shared_settings.cache_clear()
    try:
        user = await web_user(session_maker)
        token = await create_link_token(session_maker, user.id)
        bot, session = make_bot()
        await start_with(dp, bot, 555, token)
    finally:
        get_shared_settings.cache_clear()
    assert any("Аккаунт привязан" in t for t in session.texts())
    markup = [c.reply_markup for c in session.calls if getattr(c, "reply_markup", None)][-1]
    assert markup.inline_keyboard[0][0].url == "https://site.example"
    async with session_maker() as s:
        assert (await s.get(User, user.id)).telegram_id == 555
        # отдельная запись пользователя для 555 не создаётся
        assert len(list(await s.scalars(select(User)))) == 1


async def test_bot_refuses_taken_telegram_and_bad_tokens(dp, session_maker) -> None:
    await register_user(session_maker, 555, "owner")
    user = await web_user(session_maker)
    token = await create_link_token(session_maker, user.id)
    bot, session = make_bot()
    await start_with(dp, bot, 555, token)
    await start_with(dp, bot, 556, "nonsense")
    texts = " ".join(session.texts())
    assert "уже привязан к другому" in texts and "недействительна" in texts
    assert "Аккаунт привязан" not in texts


async def test_plain_start_still_registers(dp, session_maker) -> None:
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(900, "/start", "plain"))
    assert any("Привет" in t for t in session.texts())
    async with session_maker() as s:
        assert (await s.scalar(select(User).where(User.telegram_id == 900))) is not None


# --- сайт ---

SECRET = "123456:LINK-token"


@pytest.fixture
def site(session_maker):
    settings = BackendSettings(
        bot_token=SECRET, bot_username="testbot", dev_login=True, _env_file=None
    )
    transport = httpx.ASGITransport(app=create_app(session_maker, settings))
    return httpx.AsyncClient(transport=transport, base_url="https://test")


def csrf_in(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


async def dev_login(client: httpx.AsyncClient, telegram_id: int = 77) -> str:
    token = csrf_in((await client.get("/dev-login")).text)
    await client.post(
        "/dev-login", data={"telegram_id": telegram_id, "username": "d", "csrf_token": token}
    )
    return csrf_in((await client.get("/settings")).text)


async def test_settings_link_flow_on_site(site, session_maker) -> None:
    async with site as client:
        token = await dev_login(client)
        page = await client.get("/settings")
        assert "Привязан" in page.text and "Отвязать Telegram" in page.text
        # без CSRF ничего не создаётся
        assert (await client.post("/settings/telegram/link")).status_code == 403
        r = await client.post("/settings/telegram/link", data={"csrf_token": token})
        link = re.search(r"https://t\.me/testbot\?start=link_([\w-]+)", r.text)
        assert link, r.text
        async with session_maker() as s:
            row = await s.scalar(select(TelegramLinkToken))
        assert row.token_hash == hash_token(link.group(1))
        assert link.group(1) not in row.token_hash

        await client.post("/settings/notifications", data={"enabled": "0", "csrf_token": token})
        assert "выключены" in (await client.get("/settings")).text

        await client.post("/settings/telegram/unlink", data={"csrf_token": token})
        page = await client.get("/settings")
        assert "Не привязан" in page.text and "Привязать Telegram" in page.text
    async with session_maker() as s:
        user = await s.scalar(select(User))
    assert (user.telegram_id, user.telegram_linked_at, user.notify_enabled) == (None, None, False)


async def test_site_pages_work_for_user_without_telegram(site, session_maker) -> None:
    async with site as client:
        token = await dev_login(client)
        await client.post("/settings/telegram/unlink", data={"csrf_token": token})
        for path in ("/cabinet", "/settings", "/groups"):
            assert (await client.get(path)).status_code == 200, path
