"""Сценарии через диспетчер: от нажатия кнопок до записей в базе."""

import asyncio
import logging

from sqlalchemy import select

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus
from bot.services import groups as groups_svc
from bot.services import memberships as ms
from bot.services.catalog import seed_services
from bot.services.ratelimit import RateLimiter
from bot.services.users import register_user
from tests.fakes import callback_update, make_bot, message_update

OWNER, PATIENT = 100, 200
SECRET = "owner_secret"


async def press(dp, bot, user_id: int, data: str, username: str = "u") -> None:
    await dp.feed_update(bot, callback_update(user_id, data, username))


async def create_group_via_bot(dp, bot, session_maker, total: int = 3, free: int = 2) -> Group:
    await seed_services(session_maker)
    for data in ("menu:create", "cg:svc:1", "cg:next", f"cg:total:{total}", f"cg:free:{free}"):
        await press(dp, bot, OWNER, data, SECRET)
    await press(dp, bot, OWNER, "cg:day:7", SECRET)
    await press(dp, bot, OWNER, "cg:confirm", SECRET)
    async with session_maker() as s:
        return await s.scalar(select(Group))


async def test_create_group_flow_saves_group(dp, session_maker) -> None:
    bot, _ = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker)
    assert (group.slots_total, group.slots_free, group.billing_day) == (3, 2, 7)
    assert str(group.price_per_member) == "99.67"  # 299 / 3, округление вверх


async def test_create_group_back_and_cancel(dp, session_maker) -> None:
    await seed_services(session_maker)
    bot, session = make_bot()
    for data in ("menu:create", "cg:svc:1", "cg:next", "cg:back", "cg:back"):
        await press(dp, bot, OWNER, data)
    assert session.texts()[-1] == "Для какого сервиса создаём группу?"
    await press(dp, bot, OWNER, "cg:cancel")
    assert "Привет" in session.texts()[-1]
    async with session_maker() as s:
        assert await s.scalar(select(Group)) is None


async def test_double_click_and_stale_buttons_are_harmless(dp, session_maker, caplog) -> None:
    await seed_services(session_maker)
    bot, session = make_bot()
    with caplog.at_level(logging.ERROR):
        await press(dp, bot, OWNER, "menu:create")
        await press(dp, bot, OWNER, "cg:svc:1")
        await press(dp, bot, OWNER, "cg:svc:1")  # двойной клик внутри диалога
        await press(dp, bot, 999, "cg:total:3")  # кнопка без активного диалога
    assert "Тариф" in session.texts()[-2]
    assert "Привет" in session.texts()[-1]  # устаревшая кнопка вернула в меню
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_group_creation_rate_limit(dp, session_maker, monkeypatch) -> None:
    monkeypatch.setattr("bot.handlers.create_group.group_limiter", RateLimiter(0, 60))
    bot, _ = make_bot()
    await seed_services(session_maker)
    for data in ("menu:create", "cg:svc:1", "cg:next", "cg:total:3", "cg:free:2", "cg:day:7"):
        await press(dp, bot, OWNER, data)
    await press(dp, bot, OWNER, "cg:confirm")
    async with session_maker() as s:
        assert await s.scalar(select(Group)) is None
    alerts = [c for c in bot.session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert any("Слишком часто" in (c.text or "") for c in alerts)


async def test_organizer_contact_hidden_until_approved(dp, session_maker) -> None:
    bot, session = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker)

    # соискатель смотрит поиск, карточку и подаёт заявку
    for data in ("menu:find", "s:svc:1:0", f"s:grp:{group.id}:0", f"s:apply:{group.id}"):
        await press(dp, bot, PATIENT, data, "pat")
    await press(dp, bot, PATIENT, "menu:my", "pat")
    await press(dp, bot, PATIENT, f"g:view:{group.id}", "pat")
    before = "\n".join(session.texts_to(PATIENT))
    assert SECRET not in before and str(OWNER) not in before
    assert "Заявка на рассмотрении" in before

    # организатор получил заявку и принимает её
    owner_msgs = session.texts_to(OWNER)
    assert any("@pat" in t for t in owner_msgs)
    async with session_maker() as s:
        membership = await s.scalar(select(Membership))
    await press(dp, bot, OWNER, f"m:ok:{membership.id}", SECRET)

    after = "\n".join(session.texts_to(PATIENT))
    assert f"@{SECRET}" in after and "7-го числа" in after
    await press(dp, bot, PATIENT, f"g:view:{group.id}", "pat")
    assert f"@{SECRET}" in session.texts_to(PATIENT)[-1]


async def test_full_lifecycle_apply_approve_leave_close(dp, session_maker) -> None:
    bot, session = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker, total=2, free=1)
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")  # двойной клик
    async with session_maker() as s:
        memberships = (await s.scalars(select(Membership))).all()
    assert len(memberships) == 1

    await press(dp, bot, OWNER, f"m:ok:{memberships[0].id}", SECRET)
    async with session_maker() as s:
        assert (await s.get(Group, group.id)).status == GroupStatus.FULL

    await press(dp, bot, OWNER, f"g:members:{group.id}", SECRET)
    assert "@pat" in session.texts_to(OWNER)[-1]

    await press(dp, bot, PATIENT, f"g:leaveok:{group.id}", "pat")
    async with session_maker() as s:
        refreshed = await s.get(Group, group.id)
        status = (await s.get(Membership, memberships[0].id)).status
    assert (refreshed.status, refreshed.slots_free) == (GroupStatus.OPEN, 1)
    assert status == MembershipStatus.LEFT
    assert any("вышел" in t for t in session.texts_to(OWNER))

    await press(dp, bot, OWNER, f"g:closeok:{group.id}", SECRET)
    async with session_maker() as s:
        assert (await s.get(Group, group.id)).status == GroupStatus.CLOSED


async def test_stranger_cannot_manage_foreign_group(dp, session_maker) -> None:
    bot, _ = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker)
    for data in (f"g:closeok:{group.id}", f"g:members:{group.id}", f"g:leaveok:{group.id}"):
        await press(dp, bot, 555, data, "stranger")
    async with session_maker() as s:
        assert (await s.get(Group, group.id)).status == GroupStatus.OPEN


async def test_application_rate_limit(dp, session_maker, monkeypatch) -> None:
    bot, _ = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker)
    monkeypatch.setattr("bot.handlers.search.application_limiter", RateLimiter(0, 60))
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")
    async with session_maker() as s:
        assert await s.scalar(select(Membership)) is None


async def test_unreachable_organizer_is_reported_to_applicant(dp, session_maker) -> None:
    bot, session = make_bot(blocked_chats={OWNER})
    group = await create_group_via_bot(dp, bot, session_maker)
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")
    assert "не получил уведомление" in session.texts_to(PATIENT)[-1]
    async with session_maker() as s:
        assert await s.scalar(select(Membership)) is not None  # заявка сохранена


async def test_unreachable_member_is_reported_to_organizer(dp, session_maker) -> None:
    bot, session = make_bot(blocked_chats={PATIENT})
    group = await create_group_via_bot(dp, bot, session_maker)
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")
    async with session_maker() as s:
        membership = await s.scalar(select(Membership))
    await press(dp, bot, OWNER, f"m:ok:{membership.id}", SECRET)
    assert "Не удалось уведомить участника" in session.texts_to(OWNER)[-1]


async def test_my_groups_lists_both_roles(dp, session_maker) -> None:
    bot, session = make_bot()
    group = await create_group_via_bot(dp, bot, session_maker)
    await press(dp, bot, PATIENT, f"s:apply:{group.id}", "pat")
    await press(dp, bot, OWNER, "menu:my", SECRET)
    await press(dp, bot, PATIENT, "menu:my", "pat")
    assert session.texts_to(OWNER)[-1] == "Ваши группы:"
    kinds = [
        b.text
        for c in session.calls
        if getattr(c, "reply_markup", None)
        for row in c.reply_markup.inline_keyboard
        for b in row
        if "·" in b.text
    ]
    assert any("организатор" in k for k in kinds) and any("заявка отправлена" in k for k in kinds)


async def test_concurrent_first_start_creates_one_user(file_session_maker) -> None:
    await asyncio.gather(*[register_user(file_session_maker, 42, "same") for _ in range(8)])
    from bot.db.models import User

    async with file_session_maker() as s:
        assert len((await s.scalars(select(User))).all()) == 1


async def test_concurrent_duplicate_applications_create_one(file_session_maker) -> None:
    from tests.test_memberships import make_world

    _, _, group, (u1, _) = await make_world(file_session_maker, free=2)
    results = await asyncio.gather(
        *[ms.apply_to_group(file_session_maker, group.id, u1.id) for _ in range(5)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, ms.Application) for r in results) == 1
    assert all(
        isinstance(r, groups_svc.GroupError) for r in results if not isinstance(r, ms.Application)
    )
    async with file_session_maker() as s:
        assert len((await s.scalars(select(Membership))).all()) == 1


async def test_group_limit_holds_under_concurrent_creation(file_session_maker) -> None:
    from tests.test_memberships import make_world

    owner, service, *_ = await make_world(file_session_maker)
    async with file_session_maker() as s:  # убираем группу из make_world, чтобы считать с нуля
        for g in (await s.scalars(select(Group))).all():
            g.status = GroupStatus.CLOSED
        await s.commit()
    results = await asyncio.gather(
        *[
            groups_svc.create_group(file_session_maker, owner.id, service.id, 3, 2, 7)
            for _ in range(6)
        ],
        return_exceptions=True,
    )
    created = [r for r in results if isinstance(r, Group)]
    assert len(created) == groups_svc.MAX_OPEN_GROUPS
    assert all(isinstance(r, groups_svc.GroupLimitError) for r in results if r not in created)


async def test_start_registers_user_and_shows_menu(dp, session_maker) -> None:
    bot, session = make_bot()
    await dp.feed_update(bot, message_update(321, "/start", "newbie"))
    assert any("Привет" in t for t in session.texts())
    async with session_maker() as s:
        from bot.db.models import User

        assert (await s.scalar(select(User).where(User.telegram_id == 321))).username == "newbie"
