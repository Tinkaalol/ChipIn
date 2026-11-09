import hashlib
import hmac
import re
import time
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from backend.config import BackendSettings
from backend.main import create_app
from shared.db.models import Group, Membership, MembershipStatus, Service, User
from shared.services import groups as groups_svc

TOKEN = "123456:WEB-token"


def login_params(telegram_id: int, username: str | None = "ali", age: int = 5, **extra) -> dict:
    fields = {"id": str(telegram_id), "first_name": "Али", "auth_date": str(int(time.time()) - age)}
    if username:
        fields["username"] = username
    fields.update(extra)
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


@pytest.fixture
def app(session_maker):
    settings = BackendSettings(bot_token=TOKEN, bot_username="testbot", _env_file=None)
    return create_app(session_maker, settings)


def make_client(app) -> httpx.AsyncClient:
    # https, иначе httpx не вернёт Secure-cookie
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def csrf_from(client: httpx.AsyncClient, path: str = "/settings") -> str:
    page = await client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


async def seed(sm) -> int:
    async with sm() as s:
        service = Service(
            name="Netflix", plan_name="Premium", price_month=Decimal("4000"), max_members=4
        )
        s.add(service)
        await s.commit()
        return service.id


async def test_login_creates_user_once_and_sets_safe_cookie(app, session_maker) -> None:
    async with make_client(app) as client:
        r = await client.get("/auth/telegram", params=login_params(42))
        assert r.status_code == 303 and r.headers["location"] == "/cabinet"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        assert (await client.get("/cabinet")).status_code == 200
        # повторный вход находит того же пользователя и обновляет username
        async with make_client(app) as second:
            await second.get("/auth/telegram", params=login_params(42, username="ali2"))
    async with session_maker() as s:
        users = list(await s.scalars(select(User)))
    assert [(u.telegram_id, u.username) for u in users] == [(42, "ali2")]


async def test_login_rejects_bad_hash_and_stale_date(app, session_maker) -> None:
    bad = login_params(42)
    bad["hash"] = "0" * 64
    stale = login_params(43, age=301)
    async with make_client(app) as client:
        for params in (bad, stale, {k: v for k, v in login_params(44).items() if k != "hash"}):
            r = await client.get("/auth/telegram", params=params)
            assert r.status_code == 400
        assert (await client.get("/cabinet")).headers["location"] == "/login"
    async with session_maker() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 0


async def test_forged_user_id_in_login_fails(app) -> None:
    params = login_params(42)
    params["id"] = "1"  # подмена id при чужой подписи
    async with make_client(app) as client:
        assert (await client.get("/auth/telegram", params=params)).status_code == 400


async def test_pages_require_login(app) -> None:
    async with make_client(app) as client:
        for path in ("/cabinet", "/groups", "/groups/new", "/groups/1", "/settings"):
            r = await client.get(path)
            assert (r.status_code, r.headers["location"]) == (303, "/login"), path
        assert (await client.get("/")).status_code == 200
        login = await client.get("/login")
        assert 'data-telegram-login="testbot"' in login.text


async def test_post_without_csrf_is_forbidden(app, session_maker) -> None:
    service_id = await seed(session_maker)
    form = {"service_id": service_id, "slots_total": 4, "slots_free": 3, "billing_day": 5}
    async with make_client(app) as client:
        await client.get("/auth/telegram", params=login_params(42))
        token = await csrf_from(client, "/groups/new")
        for data in (form, {**form, "csrf_token": "wrong"}):
            assert (await client.post("/groups/new", data=data)).status_code == 403
        assert (await client.post("/logout")).status_code == 403
        assert (await client.get("/cabinet")).status_code == 200  # осталась в сессии
        ok = await client.post("/groups/new", data={**form, "csrf_token": token})
        assert ok.status_code == 303 and ok.headers["location"].startswith("/groups/")
        # заголовок HTMX тоже годится как носитель токена
        r = await client.post("/settings", data={"language": "en"}, headers={"X-CSRF-Token": token})
        assert r.status_code == 303
    async with session_maker() as s:
        assert await s.scalar(select(func.count()).select_from(Group)) == 1


async def test_csrf_token_from_another_session_is_rejected(app, session_maker) -> None:
    async with make_client(app) as a, make_client(app) as b:
        await a.get("/auth/telegram", params=login_params(1, "a"))
        await b.get("/auth/telegram", params=login_params(2, "b"))
        token_a = await csrf_from(a)
        r = await b.post("/settings", data={"language": "en", "csrf_token": token_a})
        assert r.status_code == 403


async def test_group_card_hides_contact_until_approved(app, session_maker) -> None:
    service_id = await seed(session_maker)
    async with make_client(app) as owner_c, make_client(app) as guest_c:
        await owner_c.get("/auth/telegram", params=login_params(1, "org_secret"))
        await guest_c.get("/auth/telegram", params=login_params(2, "guest"))
        token = await csrf_from(owner_c)
        r = await owner_c.post(
            "/groups/new",
            data={
                "service_id": service_id,
                "slots_total": 4,
                "slots_free": 3,
                "billing_day": 5,
                "csrf_token": token,
            },
        )
        card_url = r.headers["location"]
        group_id = int(card_url.rsplit("/", 1)[1])

        listing = await guest_c.get(f"/groups?service={service_id}")
        assert card_url in listing.text and "org_secret" not in listing.text
        card = await guest_c.get(card_url)
        assert card.status_code == 200 and "org_secret" not in card.text

        gtoken = await csrf_from(guest_c)
        await guest_c.post(f"{card_url}/join", data={"csrf_token": gtoken})
        pending = await guest_c.get(card_url)
        assert "ждите ответа" in pending.text and "org_secret" not in pending.text

        # заявку «принимает» не организатор: сервер отклоняет
        async with session_maker() as s:
            mid = await s.scalar(select(func.max(Membership.id)))
        await guest_c.post(
            f"{card_url}/applications/{mid}", data={"csrf_token": gtoken, "decision": "approve"}
        )
        assert "org_secret" not in (await guest_c.get(card_url)).text
        async with session_maker() as s:
            assert (await s.get(Membership, mid)).status == MembershipStatus.PENDING

        otoken = await csrf_from(owner_c)
        await owner_c.post(
            f"{card_url}/applications/{mid}", data={"csrf_token": otoken, "decision": "approve"}
        )
        approved = await guest_c.get(card_url)
        assert "https://t.me/org_secret" in approved.text
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.APPROVED
        guest_id = (await s.get(Membership, mid)).user_id
    assert (
        await groups_svc.get_user_role(session_maker, group_id, guest_id) == groups_svc.Role.MEMBER
    )


async def test_banned_user_session_is_dropped(app, session_maker) -> None:
    async with make_client(app) as client:
        await client.get("/auth/telegram", params=login_params(42))
        async with session_maker() as s:
            user = await s.scalar(select(User))
            user.is_banned = True
            await s.commit()
        assert (await client.get("/cabinet")).headers["location"] == "/login"
        r = await client.get("/auth/telegram", params=login_params(42))
        assert r.status_code == 403


async def test_dev_login_is_off_by_default_and_with_public_url(app, session_maker) -> None:
    async with make_client(app) as client:
        assert (await client.get("/dev-login")).status_code == 404
        assert (await client.post("/dev-login", data={"telegram_id": 5})).status_code in (403, 404)
    for extra in ({"dev_login": True, "public_url": "https://example.com"},):
        settings = BackendSettings(bot_token=TOKEN, _env_file=None, **extra)
        async with make_client(create_app(session_maker, settings)) as client:
            assert (await client.get("/dev-login")).status_code == 404


async def test_dev_login_works_when_enabled(session_maker) -> None:
    settings = BackendSettings(bot_token=TOKEN, dev_login=True, _env_file=None)
    async with make_client(create_app(session_maker, settings)) as client:
        token = (
            (await client.get("/dev-login"))
            .text.split('name="csrf_token" value="')[1]
            .split('"')[0]
        )
        r = await client.post(
            "/dev-login", data={"telegram_id": 5, "username": "@dev", "csrf_token": token}
        )
        assert r.headers["location"] == "/cabinet"
    async with session_maker() as s:
        assert (await s.scalar(select(User))).username == "dev"
