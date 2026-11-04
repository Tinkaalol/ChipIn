import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from bot.db.models import Base, Group, GroupStatus, Membership, MembershipStatus, Service, User
from bot.db.session import make_engine, make_session_maker
from bot.services import groups as groups_svc
from bot.services import memberships as svc


async def make_world(sm, free: int = 1, total: int = 3, applicants: int = 2):
    """Организатор, сервис, группа и несколько пользователей без заявок."""
    async with sm() as s:
        owner = User(telegram_id=100, username="owner")
        service = Service(
            name="Spotify", plan_name="Family", price_month=Decimal("300"), max_members=6
        )
        users = [User(telegram_id=200 + i, username=f"u{i}") for i in range(applicants)]
        s.add_all([owner, service, *users])
        await s.flush()
        group = Group(
            service_id=service.id,
            owner_id=owner.id,
            price_per_member=Decimal("100"),
            slots_total=total,
            slots_free=free,
            billing_day=10,
        )
        s.add(group)
        await s.commit()
        return owner, service, group, users


async def reload(sm, model, pk):
    async with sm() as s:
        return await s.get(model, pk)


async def test_apply_creates_pending_membership(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    assert app.membership.status == MembershipStatus.PENDING
    assert app.owner.id == owner.id


async def test_apply_rejections(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker)
    with pytest.raises(groups_svc.GroupError, match="собственная"):
        await groups_svc.request_join(session_maker, group.id, owner.id)
    await groups_svc.request_join(session_maker, group.id, u1.id)
    with pytest.raises(groups_svc.GroupError, match="уже подали"):
        await groups_svc.request_join(session_maker, group.id, u1.id)
    async with session_maker() as s:
        (await s.get(User, u2.id)).is_banned = True
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match="недоступна"):
        await groups_svc.request_join(session_maker, group.id, u2.id)
    with pytest.raises(groups_svc.GroupError, match="не найдена"):
        await groups_svc.request_join(session_maker, 999, u1.id)


async def test_approve_takes_slot_and_fills_group(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=1)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    d = await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    assert d.approved and d.member.id == u1.id
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 0
    assert g.status == GroupStatus.FULL
    assert m.status == MembershipStatus.APPROVED and m.joined_at is not None


async def test_approve_keeps_group_open_while_slots_remain(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    g = await reload(session_maker, Group, group.id)
    assert g.slots_free == 1 and g.status == GroupStatus.OPEN


async def test_second_approval_for_last_slot_fails(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=1)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    a2 = await groups_svc.request_join(session_maker, group.id, u2.id)
    await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    with pytest.raises(groups_svc.GroupError, match="мест не осталось"):
        await groups_svc.approve_member(session_maker, a2.membership.id, owner.id)
    g = await reload(session_maker, Group, group.id)
    m2 = await reload(session_maker, Membership, a2.membership.id)
    assert g.slots_free == 0
    assert m2.status == MembershipStatus.PENDING


async def test_concurrent_approvals_for_last_slot(tmp_path) -> None:
    """Два одновременных одобрения на единственное место: проходит ровно одно."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_session_maker(engine)
    owner, _, group, (u1, u2) = await make_world(sm, free=1)
    a1 = await groups_svc.request_join(sm, group.id, u1.id)
    a2 = await groups_svc.request_join(sm, group.id, u2.id)
    results = await asyncio.gather(
        groups_svc.approve_member(sm, a1.membership.id, owner.id),
        groups_svc.approve_member(sm, a2.membership.id, owner.id),
        return_exceptions=True,
    )
    await engine.dispose()
    ok = [r for r in results if isinstance(r, groups_svc.Decision)]
    failed = [r for r in results if isinstance(r, groups_svc.GroupError)]
    assert len(ok) == 1 and len(failed) == 1


async def test_only_owner_decides_and_only_once(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    with pytest.raises(groups_svc.GroupError, match="не ваша"):
        await groups_svc.approve_member(session_maker, app.membership.id, u2.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    with pytest.raises(groups_svc.GroupError, match="уже обработана"):
        await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    with pytest.raises(groups_svc.GroupError, match="не найдена"):
        await groups_svc.approve_member(session_maker, 999, owner.id)


async def test_reject_keeps_slots_and_blocks_reapply(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    d = await groups_svc.reject_member(session_maker, app.membership.id, owner.id)
    assert not d.approved
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 2 and m.status == MembershipStatus.REJECTED
    with pytest.raises(groups_svc.GroupError, match="отклонил"):
        await groups_svc.request_join(session_maker, group.id, u1.id)


async def test_leave_returns_slot_and_reopens_full_group(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=1)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    dep = await svc.leave_group(session_maker, group.id, u1.id)
    assert dep.was_approved
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 1 and g.status == GroupStatus.OPEN
    assert m.status == MembershipStatus.LEFT
    # после выхода можно подать заявку снова
    await groups_svc.request_join(session_maker, group.id, u1.id)


async def test_leave_pending_does_not_touch_slots(session_maker) -> None:
    _, _, group, (u1, _) = await make_world(session_maker, free=2)
    await groups_svc.request_join(session_maker, group.id, u1.id)
    dep = await svc.leave_group(session_maker, group.id, u1.id)
    assert not dep.was_approved
    assert (await reload(session_maker, Group, group.id)).slots_free == 2


async def test_leave_when_not_member(session_maker) -> None:
    _, _, group, (u1, _) = await make_world(session_maker)
    with pytest.raises(groups_svc.GroupError, match="не состоите"):
        await svc.leave_group(session_maker, group.id, u1.id)


async def test_close_group(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    a2 = await groups_svc.request_join(session_maker, group.id, u2.id)
    with pytest.raises(groups_svc.GroupError, match="не ваша"):
        await svc.close_group(session_maker, group.id, u1.id)
    closure = await svc.close_group(session_maker, group.id, owner.id)
    assert sorted(closure.notify_ids) == [u1.telegram_id, u2.telegram_id]
    assert (await reload(session_maker, Group, group.id)).status == GroupStatus.CLOSED
    m2 = await reload(session_maker, Membership, a2.membership.id)
    assert m2.status == MembershipStatus.REJECTED
    with pytest.raises(groups_svc.GroupError, match="уже закрыта"):
        await svc.close_group(session_maker, group.id, owner.id)
    assert await svc.list_user_groups(session_maker, owner.id) == []


async def test_members_list_owner_only(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.request_join(session_maker, group.id, u2.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    members = await svc.list_members(session_maker, group.id, owner.id)
    assert [m.id for m in members] == [u1.id]
    with pytest.raises(groups_svc.GroupError):
        await svc.list_members(session_maker, group.id, u1.id)


async def test_user_groups_cover_owner_and_member_roles(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    await groups_svc.request_join(session_maker, group.id, u1.id)
    mine = await svc.list_user_groups(session_maker, owner.id)
    theirs = await svc.list_user_groups(session_maker, u1.id)
    assert [e.role for e in mine] == ["owner"]
    assert [(e.role, e.membership_status) for e in theirs] == [("member", MembershipStatus.PENDING)]


async def test_cannot_join_closed_or_full_group(session_maker) -> None:
    _, _, group, (u1, u2) = await make_world(session_maker, free=2)
    async with session_maker() as s:
        g = await s.get(Group, group.id)
        g.status = GroupStatus.FULL
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match="нет свободных мест"):
        await groups_svc.request_join(session_maker, group.id, u1.id)
    async with session_maker() as s:
        g = await s.get(Group, group.id)
        g.status = GroupStatus.CLOSED
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match="закрыта"):
        await groups_svc.request_join(session_maker, group.id, u2.id)


async def test_cannot_join_when_already_member(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    with pytest.raises(groups_svc.GroupError, match="уже участник"):
        await groups_svc.request_join(session_maker, group.id, u1.id)


async def test_reapply_after_rejection_only_after_24_hours(session_maker) -> None:
    from datetime import UTC, datetime, timedelta

    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    decision = await groups_svc.reject_member(session_maker, app.membership.id, owner.id)
    assert decision.membership.decided_at is not None

    with pytest.raises(groups_svc.GroupError, match=r"через 24 ч"):
        await groups_svc.request_join(session_maker, group.id, u1.id)

    async with session_maker() as s:  # отказ был 23 часа назад: осталось ждать час
        (await s.get(Membership, app.membership.id)).decided_at = datetime.now(UTC) - timedelta(
            hours=23
        )
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match=r"через 1 ч"):
        await groups_svc.request_join(session_maker, group.id, u1.id)

    async with session_maker() as s:  # прошло больше суток
        (await s.get(Membership, app.membership.id)).decided_at = datetime.now(UTC) - timedelta(
            hours=25
        )
        await s.commit()
    again = await groups_svc.request_join(session_maker, group.id, u1.id)
    assert again.membership.id != app.membership.id
    assert again.membership.status == MembershipStatus.PENDING
    # новая отказанная заявка снова включает паузу
    await groups_svc.reject_member(session_maker, again.membership.id, owner.id)
    with pytest.raises(groups_svc.GroupError, match="отклонил"):
        await groups_svc.request_join(session_maker, group.id, u1.id)


async def test_approve_sets_decision_time_and_reject_keeps_slots(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    a2 = await groups_svc.request_join(session_maker, group.id, u2.id)
    ok = await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    no = await groups_svc.reject_member(session_maker, a2.membership.id, owner.id)
    assert ok.membership.decided_at is not None and ok.membership.joined_at is not None
    assert no.membership.joined_at is None and no.group.slots_free == 1


async def test_many_concurrent_approvals_for_one_slot(file_session_maker) -> None:
    sm = file_session_maker
    owner, _, group, users = await make_world(sm, free=1, total=3, applicants=5)
    apps = [await groups_svc.request_join(sm, group.id, u.id) for u in users]
    results = await asyncio.gather(
        *[groups_svc.approve_member(sm, a.membership.id, owner.id) for a in apps],
        return_exceptions=True,
    )
    won = [r for r in results if isinstance(r, groups_svc.Decision)]
    assert len(won) == 1
    assert all(isinstance(r, groups_svc.GroupError) for r in results if r not in won)
    g = await reload(sm, Group, group.id)
    assert g.slots_free == 0 and g.status == GroupStatus.FULL
    async with sm() as s:
        approved = (
            await s.scalars(
                select(Membership).where(Membership.status == MembershipStatus.APPROVED)
            )
        ).all()
    assert len(approved) == 1
