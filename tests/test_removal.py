from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Report, User
from bot.services import groups as groups_svc
from bot.services import memberships as ms
from bot.services import moderation as mod
from tests.fakes import callback_update, make_bot, message_update
from tests.test_memberships import make_world, reload


async def joined(sm, free: int = 1, total: int = 3):
    """Группа, в которой участник u1 уже принят."""
    owner, service, group, (u1, u2) = await make_world(sm, free=free, total=total)
    app = await groups_svc.request_join(sm, group.id, u1.id)
    await groups_svc.approve_member(sm, app.membership.id, owner.id)
    return owner, service, group, u1, u2, app.membership.id


async def test_remove_frees_slot_and_reopens_full_group(session_maker) -> None:
    owner, _, group, u1, _, mid = await joined(session_maker, free=1)
    assert (await reload(session_maker, Group, group.id)).status == GroupStatus.FULL

    removal = await ms.remove_member(session_maker, mid, owner.id)

    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, mid)
    assert (g.slots_free, g.status) == (1, GroupStatus.OPEN)
    assert m.status == MembershipStatus.REMOVED
    assert m.removed_by == owner.id and m.removed_at is not None
    assert removal.member.id == u1.id and removal.owner.id == owner.id
    assert await groups_svc.get_user_role(session_maker, group.id, u1.id) == groups_svc.Role.NONE
    assert await ms.list_members(session_maker, group.id, owner.id) == []


async def test_remove_keeps_open_group_open_and_adds_one_slot(session_maker) -> None:
    owner, _, group, _, _, mid = await joined(session_maker, free=2, total=4)
    await ms.remove_member(session_maker, mid, owner.id)
    g = await reload(session_maker, Group, group.id)
    assert (g.slots_free, g.status) == (2, GroupStatus.OPEN)


async def test_only_group_owner_can_remove(session_maker) -> None:
    owner, _, group, u1, u2, mid = await joined(session_maker)
    other_owner, *_ = await _second_owner_group(session_maker)
    for actor in (u2.id, u1.id, other_owner):  # посторонний, сам участник, организатор чужой группы
        with pytest.raises(groups_svc.GroupError, match="не ваша"):
            await ms.remove_member(session_maker, mid, actor)
        with pytest.raises(groups_svc.GroupError, match="не ваша"):
            await ms.get_removable_member(session_maker, mid, actor)
    m = await reload(session_maker, Membership, mid)
    assert m.status == MembershipStatus.APPROVED
    assert (await reload(session_maker, Group, group.id)).slots_free == 0


async def _second_owner_group(sm):
    from decimal import Decimal

    from bot.db.models import Service

    async with sm() as s:
        other = User(telegram_id=900, username="other_owner")
        service = Service(name="X", plan_name="Y", price_month=Decimal("10"), max_members=4)
        s.add_all([other, service])
        await s.flush()
        s.add(
            Group(
                service_id=service.id,
                owner_id=other.id,
                price_per_member=Decimal("5"),
                slots_total=3,
                slots_free=2,
                billing_day=3,
            )
        )
        await s.commit()
        return other.id, service.id


async def test_cannot_remove_twice_self_or_non_member(session_maker) -> None:
    owner, _, group, u1, u2, mid = await joined(session_maker, free=2, total=4)
    await ms.remove_member(session_maker, mid, owner.id)
    with pytest.raises(groups_svc.GroupError, match="нет в группе"):
        await ms.remove_member(session_maker, mid, owner.id)  # повторное удаление
    assert (await reload(session_maker, Group, group.id)).slots_free == 2  # место не задвоилось

    pending = await groups_svc.request_join(session_maker, group.id, u2.id)
    with pytest.raises(groups_svc.GroupError, match="нет в группе"):
        await ms.remove_member(session_maker, pending.membership.id, owner.id)  # ждущая заявка

    with pytest.raises(groups_svc.GroupError, match="не найден"):
        await ms.remove_member(session_maker, 9999, owner.id)

    async with session_maker() as s:  # запись участия самого организатора
        own = Membership(group_id=group.id, user_id=owner.id, status=MembershipStatus.APPROVED)
        s.add(own)
        await s.commit()
        own_id = own.id
    with pytest.raises(groups_svc.GroupError, match="Себя"):
        await ms.remove_member(session_maker, own_id, owner.id)


async def test_removed_member_cannot_reapply_for_7_days(session_maker) -> None:
    owner, _, group, u1, _, mid = await joined(session_maker)
    await ms.remove_member(session_maker, mid, owner.id)

    with pytest.raises(groups_svc.GroupError, match=r"удалил вас.*через 7 дн"):
        await groups_svc.request_join(session_maker, group.id, u1.id)

    async with session_maker() as s:  # прошло 6 суток: осталось меньше суток
        (await s.get(Membership, mid)).removed_at = datetime.now(UTC) - timedelta(days=6, hours=1)
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match=r"через 1 дн"):
        await groups_svc.request_join(session_maker, group.id, u1.id)

    async with session_maker() as s:  # прошло больше недели
        (await s.get(Membership, mid)).removed_at = datetime.now(UTC) - timedelta(days=8)
        await s.commit()
    again = await groups_svc.request_join(session_maker, group.id, u1.id)
    assert again.membership.status == MembershipStatus.PENDING


async def test_removed_group_is_hidden_from_search_for_7_days(session_maker) -> None:
    owner, service, group, u1, _, mid = await joined(session_maker, free=2, total=4)
    await ms.remove_member(session_maker, mid, owner.id)
    assert await groups_svc.list_open_groups(session_maker, service.id, u1.id, 0, 5) == []
    async with session_maker() as s:
        (await s.get(Membership, mid)).removed_at = datetime.now(UTC) - timedelta(days=8)
        await s.commit()
    found = await groups_svc.list_open_groups(session_maker, service.id, u1.id, 0, 5)
    assert [g.id for g in found] == [group.id]


async def test_removal_reason_is_optional_and_owner_only(session_maker) -> None:
    owner, _, _, u1, u2, mid = await joined(session_maker, free=2, total=4)
    with pytest.raises(groups_svc.GroupError, match="только для участника"):
        await ms.set_removal_reason(session_maker, mid, owner.id, "рано")  # ещё не удалён
    await ms.remove_member(session_maker, mid, owner.id)
    assert (await reload(session_maker, Membership, mid)).removal_reason is None  # без причины

    with pytest.raises(groups_svc.GroupError, match="только для участника"):
        await ms.set_removal_reason(session_maker, mid, u2.id, "чужая")
    with pytest.raises(groups_svc.GroupError, match="пустой"):
        await ms.set_removal_reason(session_maker, mid, owner.id, "   ")
    with pytest.raises(groups_svc.GroupError, match="длинная"):
        await ms.set_removal_reason(session_maker, mid, owner.id, "x" * 301)
    removal = await ms.set_removal_reason(session_maker, mid, owner.id, " Не платил ")
    assert removal.membership.removal_reason == "Не платил"
    with pytest.raises(groups_svc.GroupError, match="уже указана"):
        await ms.set_removal_reason(session_maker, mid, owner.id, "ещё раз")


async def test_removed_member_can_report_the_organizer(session_maker) -> None:
    owner, _, group, u1, u2, mid = await joined(session_maker, free=2, total=4)
    await ms.remove_member(session_maker, mid, owner.id)
    report = await mod.create_report(session_maker, u1.id, group.id, owner.id, "other", "выгнали")
    assert report.target_user_id == owner.id
    # посторонний, который никогда не был в группе, по-прежнему не может
    with pytest.raises(groups_svc.GroupError, match="своей группы"):
        await mod.create_report(session_maker, u2.id, group.id, owner.id, "other", "x")
    async with session_maker() as s:
        assert len((await s.scalars(select(Report))).all()) == 1


async def test_concurrent_double_removal_frees_one_slot(file_session_maker) -> None:
    import asyncio

    sm = file_session_maker
    owner, _, group, u1, _, mid = await joined(sm, free=2, total=4)
    results = await asyncio.gather(
        *[ms.remove_member(sm, mid, owner.id) for _ in range(4)], return_exceptions=True
    )
    assert sum(isinstance(r, ms.Removal) for r in results) == 1
    assert (await reload(sm, Group, group.id)).slots_free == 2


# ---- через диспетчер: подтверждение, уведомление, причина, подмена callback_data


async def test_removal_flow_through_bot(dp, session_maker) -> None:
    owner, _, group, u1, _, mid = await joined(session_maker)
    bot, session = make_bot()
    o = owner.telegram_id
    await dp.feed_update(bot, callback_update(o, f"g:members:{group.id}", "owner"))
    markup = [c for c in session.calls if getattr(c, "reply_markup", None)][-1].reply_markup
    labels = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    assert ("Удалить @u0", f"g:rm:{mid}") in labels

    await dp.feed_update(bot, callback_update(o, f"g:rm:{mid}", "owner"))
    assert session.texts_to(o)[-1] == "Удалить @u0 из группы? Он получит уведомление."
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.APPROVED  # пока нет

    await dp.feed_update(bot, callback_update(o, f"g:rmok:{mid}", "owner"))
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.REMOVED
    assert (
        session.texts_to(u1.telegram_id)[-1] == "Организатор удалил вас из группы Spotify Family."
    )
    assert "причину" in session.texts_to(o)[-1]

    await dp.feed_update(bot, message_update(o, "Не платил два месяца", "owner"))
    assert "Не платил два месяца" in session.texts_to(u1.telegram_id)[-1]
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).removal_reason == "Не платил два месяца"


async def test_removal_without_reason_and_cancel(dp, session_maker) -> None:
    owner, _, group, u1, _, mid = await joined(session_maker)
    bot, session = make_bot()
    o = owner.telegram_id
    await dp.feed_update(bot, callback_update(o, f"g:rm:{mid}", "owner"))
    await dp.feed_update(bot, callback_update(o, f"g:members:{group.id}", "owner"))  # «Отмена»
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.APPROVED
    await dp.feed_update(bot, callback_update(o, f"g:rmok:{mid}", "owner"))
    await dp.feed_update(bot, callback_update(o, "g:rmskip", "owner"))
    assert session.texts_to(o)[-1] == "Пока нет принятых участников."
    assert all("Причина" not in t for t in session.texts_to(u1.telegram_id))


async def test_forged_callback_cannot_remove_foreign_member(dp, session_maker) -> None:
    owner, _, group, u1, u2, mid = await joined(session_maker)
    bot, session = make_bot()
    for data in (f"g:rm:{mid}", f"g:rmok:{mid}"):
        await dp.feed_update(bot, callback_update(u2.telegram_id, data, "stranger"))
        await dp.feed_update(bot, callback_update(u1.telegram_id, data, "u0"))  # сам участник
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.APPROVED
        assert (await s.get(Group, group.id)).slots_free == 0
    assert session.texts_to(u1.telegram_id) == []  # уведомлений об удалении нет
    alerts = [c for c in session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert alerts and all(c.show_alert for c in alerts)


async def test_removed_member_can_file_complaint_through_bot(dp, session_maker) -> None:
    owner, _, group, u1, _, mid = await joined(session_maker)
    bot, session = make_bot()
    await dp.feed_update(bot, callback_update(owner.telegram_id, f"g:rmok:{mid}", "owner"))
    complaint = f"r:new:{group.id}:{owner.id}"
    sent = [c for c in session.calls if getattr(c, "chat_id", None) == u1.telegram_id][-1]
    assert complaint in str(sent.reply_markup)
    member = u1.telegram_id
    await dp.feed_update(bot, callback_update(member, complaint, "u0"))
    await dp.feed_update(bot, callback_update(member, "r:reason:other", "u0"))
    await dp.feed_update(bot, message_update(member, "удалили без причины", "u0"))
    async with session_maker() as s:
        report = await s.scalar(select(Report))
    assert report.reporter_id == u1.id and report.target_user_id == owner.id
