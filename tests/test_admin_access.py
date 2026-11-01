import pytest
from aiogram import Dispatcher
from sqlalchemy import select

from bot.db.models import Report, ReportStatus, Service, User
from bot.handlers import admin, create_group, errors, my_groups, report, search, start
from bot.main import build_dispatcher
from bot.services import catalog
from bot.services import moderation as mod
from bot.services.ratelimit import RateLimiter
from tests.fakes import callback_update, make_bot, message_update
from tests.test_memberships import make_world

ADMIN_ID = 777
STRANGER_ID = 888


@pytest.fixture
def dp(session_maker, monkeypatch) -> Dispatcher:
    for module in (
        "bot.handlers.admin",
        "bot.handlers.start",
        "bot.handlers.report",
        "bot.handlers.my_groups",
    ):
        monkeypatch.setattr(f"{module}.session_maker", session_maker)
    monkeypatch.setattr("bot.handlers.admin.admin_ids", lambda: {ADMIN_ID})
    monkeypatch.setattr(
        "bot.handlers.report.get_settings", lambda: type("S", (), {"admin_ids": [ADMIN_ID]})()
    )
    # Роутеры модульные, поэтому перед новой сборкой диспетчера отцепляем их от старого
    for module in (admin, create_group, errors, my_groups, report, search, start):
        module.router._parent_router = None
    return build_dispatcher(session_maker)


async def test_admin_commands_ignored_for_non_admin(dp, session_maker) -> None:
    owner, *_ = await make_world(session_maker)
    bot, session = make_bot()
    for text in ("/reports", "/ban @owner", "/unban @owner", "/services"):
        await dp.feed_update(bot, message_update(STRANGER_ID, text))
    assert session.texts() == []  # бот не раскрывает админские команды
    assert not (await _user(session_maker, owner.id)).is_banned


async def test_admin_callbacks_do_not_work_for_non_admin(dp, session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    report = await _make_report(session_maker, group.id, u1.id, owner.id)
    bot, _ = make_bot()
    await dp.feed_update(bot, callback_update(STRANGER_ID, f"a:ban:{report.id}"))
    async with session_maker() as s:
        assert (await s.get(Report, report.id)).status == ReportStatus.NEW
    assert not (await _user(session_maker, owner.id)).is_banned


async def test_admin_can_ban_and_unban_by_username(dp, session_maker) -> None:
    _, _, _, (u1, _) = await make_world(session_maker)
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(ADMIN_ID, "/ban @u0"))
    assert (await _user(session_maker, u1.id)).is_banned
    assert any("забанен" in t for t in session.texts())
    await dp.feed_update(bot, message_update(ADMIN_ID, "/unban @u0"))
    assert not (await _user(session_maker, u1.id)).is_banned


async def test_admin_cannot_ban_admin(dp, session_maker) -> None:
    async with session_maker() as s:
        s.add(User(telegram_id=ADMIN_ID, username="boss"))
        await s.commit()
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(ADMIN_ID, "/ban @boss"))
    assert any("Администратора" in t for t in session.texts())
    async with session_maker() as s:
        assert not (await s.scalar(select(User).where(User.telegram_id == ADMIN_ID))).is_banned


async def test_reports_flow_for_admin(dp, session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    report = await _make_report(session_maker, group.id, u1.id, owner.id)
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(ADMIN_ID, "/reports"))
    assert any(f"Жалоба #{report.id}" in t for t in session.texts())
    await dp.feed_update(bot, callback_update(ADMIN_ID, f"a:dismiss:{report.id}"))
    async with session_maker() as s:
        assert (await s.get(Report, report.id)).status == ReportStatus.DISMISSED
    await dp.feed_update(bot, message_update(ADMIN_ID, "/reports"))
    assert session.texts()[-1] == "Открытых жалоб нет."


async def test_admin_manages_services(dp, session_maker) -> None:
    await make_world(session_maker)
    bot, _ = make_bot()
    await dp.feed_update(bot, callback_update(ADMIN_ID, "a:svc:add"))
    await dp.feed_update(bot, message_update(ADMIN_ID, "Netflix | Premium | 999,5 | 4"))
    async with session_maker() as s:
        netflix = await s.scalar(select(Service).where(Service.name == "Netflix"))
    assert netflix is not None and str(netflix.price_month) == "999.50"

    await dp.feed_update(bot, callback_update(ADMIN_ID, f"a:svc:price:{netflix.id}"))
    await dp.feed_update(bot, message_update(ADMIN_ID, "1200"))
    await dp.feed_update(bot, callback_update(ADMIN_ID, f"a:svc:toggle:{netflix.id}"))
    async with session_maker() as s:
        netflix = await s.get(Service, netflix.id)
    assert str(netflix.price_month) == "1200.00" and netflix.is_active is False


async def test_non_admin_cannot_change_services(dp, session_maker) -> None:
    _, service, *_ = await make_world(session_maker)
    bot, _ = make_bot()
    await dp.feed_update(bot, callback_update(STRANGER_ID, f"a:svc:toggle:{service.id}"))
    async with session_maker() as s:
        assert (await s.get(Service, service.id)).is_active is True


async def test_banned_user_is_blocked_everywhere(dp, session_maker) -> None:
    _, _, _, (u1, _) = await make_world(session_maker)
    await mod.ban_user(session_maker, u1.id, protected_ids=set())
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(u1.telegram_id, "/start"))
    await dp.feed_update(bot, callback_update(u1.telegram_id, "menu:find"))
    assert session.texts() == ["Доступ к боту ограничен."]  # только отказ, меню не показано
    assert session.alerts() == ["Доступ к боту ограничен."]  # на кнопку отвечает всплывающее окно
    assert any(type(c).__name__ == "AnswerCallbackQuery" and c.show_alert for c in session.calls)


async def test_unbanned_user_can_start(dp, session_maker) -> None:
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(555, "/start"))
    assert any("Привет" in t for t in session.texts())


async def test_catalog_validation(session_maker) -> None:
    from bot.services.groups import GroupError

    with pytest.raises(GroupError):
        catalog.parse_price("abc")
    with pytest.raises(GroupError):
        catalog.parse_price("-1")
    with pytest.raises(GroupError):
        await catalog.add_service(session_maker, "A", "B", catalog.parse_price("1"), 1)
    await catalog.add_service(session_maker, "A", "B", catalog.parse_price("1"), 3)
    with pytest.raises(GroupError, match="уже есть"):
        await catalog.add_service(session_maker, "A", "B", catalog.parse_price("1"), 3)


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter(limit=2, window=10, clock=lambda: now[0])
    assert limiter.allow(1) and limiter.allow(1)
    assert not limiter.allow(1)
    assert limiter.allow(2)  # другой пользователь не затронут
    now[0] = 10.0
    assert limiter.allow(1)  # окно прошло
    assert limiter.allow(1)
    assert not limiter.allow(1)


async def _user(sm, user_id: int) -> User:
    async with sm() as s:
        return await s.get(User, user_id)


async def _make_report(sm, group_id: int, reporter_id: int, target_id: int) -> Report:
    async with sm() as s:
        report = Report(
            reporter_id=reporter_id, target_user_id=target_id, group_id=group_id, reason="Не платит"
        )
        s.add(report)
        await s.commit()
        return report


async def test_member_reports_organizer_through_bot(dp, session_maker) -> None:
    from bot.services import memberships as ms

    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await ms.apply_to_group(session_maker, group.id, u1.id)
    await ms.decide_application(session_maker, app.membership.id, owner.id, True)
    bot, session = make_bot()
    member = u1.telegram_id

    await dp.feed_update(bot, callback_update(member, f"g:view:{group.id}"))
    assert any(
        f"r:new:{group.id}:{owner.id}" in str(m.reply_markup)
        for m in session.calls
        if hasattr(m, "reply_markup") and m.reply_markup
    )
    await dp.feed_update(bot, callback_update(member, f"r:new:{group.id}:{owner.id}"))
    await dp.feed_update(bot, callback_update(member, "r:reason:unpaid"))
    await dp.feed_update(bot, message_update(member, "не отвечает неделю"))

    async with session_maker() as s:
        report = await s.scalar(select(Report))
    assert report.reason == "Не платит: не отвечает неделю"
    assert report.reporter_id == u1.id and report.target_user_id == owner.id
    sent_to_admin = [m for m in session.calls if getattr(m, "chat_id", None) == ADMIN_ID]
    assert sent_to_admin and "/reports" in sent_to_admin[0].text


async def test_report_flow_back_and_cancel(dp, session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    from bot.services import memberships as ms

    app = await ms.apply_to_group(session_maker, group.id, u1.id)
    await ms.decide_application(session_maker, app.membership.id, owner.id, True)
    bot, session = make_bot()
    member = u1.telegram_id
    await dp.feed_update(bot, callback_update(member, f"r:new:{group.id}:{owner.id}"))
    await dp.feed_update(bot, callback_update(member, "r:reason:other"))
    await dp.feed_update(bot, callback_update(member, "r:back"))
    assert session.texts()[-1] == "Что случилось?"
    await dp.feed_update(bot, callback_update(member, "r:back"))
    assert "Spotify" in session.texts()[-1]  # вернулись в карточку группы
    await dp.feed_update(bot, callback_update(member, f"r:new:{group.id}:{owner.id}"))
    await dp.feed_update(bot, callback_update(member, "r:cancel"))
    assert "Привет" in session.texts()[-1]
    async with session_maker() as s:
        assert await s.scalar(select(Report)) is None
