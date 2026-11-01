import asyncio
from decimal import Decimal

import pytest

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
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    assert app.membership.status == MembershipStatus.PENDING
    assert app.owner.id == owner.id


async def test_apply_rejections(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker)
    with pytest.raises(groups_svc.GroupError, match="собственная"):
        await svc.apply_to_group(session_maker, group.id, owner.id)
    await svc.apply_to_group(session_maker, group.id, u1.id)
    with pytest.raises(groups_svc.GroupError, match="уже подали"):
        await svc.apply_to_group(session_maker, group.id, u1.id)
    async with session_maker() as s:
        (await s.get(User, u2.id)).is_banned = True
        await s.commit()
    with pytest.raises(groups_svc.GroupError, match="недоступна"):
        await svc.apply_to_group(session_maker, group.id, u2.id)
    with pytest.raises(groups_svc.GroupError, match="нет мест"):
        await svc.apply_to_group(session_maker, 999, u1.id)


async def test_approve_takes_slot_and_fills_group(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=1)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    d = await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    assert d.approved and d.member.id == u1.id
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 0
    assert g.status == GroupStatus.FULL
    assert m.status == MembershipStatus.APPROVED and m.joined_at is not None


async def test_approve_keeps_group_open_while_slots_remain(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    g = await reload(session_maker, Group, group.id)
    assert g.slots_free == 1 and g.status == GroupStatus.OPEN


async def test_second_approval_for_last_slot_fails(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=1)
    a1 = await svc.apply_to_group(session_maker, group.id, u1.id)
    a2 = await svc.apply_to_group(session_maker, group.id, u2.id)
    await svc.decide_application(session_maker, a1.membership.id, owner.id, True)
    with pytest.raises(groups_svc.GroupError, match="мест не осталось"):
        await svc.decide_application(session_maker, a2.membership.id, owner.id, True)
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
    a1 = await svc.apply_to_group(sm, group.id, u1.id)
    a2 = await svc.apply_to_group(sm, group.id, u2.id)
    results = await asyncio.gather(
        svc.decide_application(sm, a1.membership.id, owner.id, True),
        svc.decide_application(sm, a2.membership.id, owner.id, True),
        return_exceptions=True,
    )
    await engine.dispose()
    ok = [r for r in results if isinstance(r, svc.Decision)]
    failed = [r for r in results if isinstance(r, groups_svc.GroupError)]
    assert len(ok) == 1 and len(failed) == 1


async def test_only_owner_decides_and_only_once(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    with pytest.raises(groups_svc.GroupError, match="не ваша"):
        await svc.decide_application(session_maker, app.membership.id, u2.id, True)
    await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    with pytest.raises(groups_svc.GroupError, match="уже обработана"):
        await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    with pytest.raises(groups_svc.GroupError, match="не найдена"):
        await svc.decide_application(session_maker, 999, owner.id, True)


async def test_reject_keeps_slots_and_blocks_reapply(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    d = await svc.decide_application(session_maker, app.membership.id, owner.id, False)
    assert not d.approved
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 2 and m.status == MembershipStatus.REJECTED
    with pytest.raises(groups_svc.GroupError, match="отклонил"):
        await svc.apply_to_group(session_maker, group.id, u1.id)


async def test_leave_returns_slot_and_reopens_full_group(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=1)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    dep = await svc.leave_group(session_maker, group.id, u1.id)
    assert dep.was_approved
    g = await reload(session_maker, Group, group.id)
    m = await reload(session_maker, Membership, app.membership.id)
    assert g.slots_free == 1 and g.status == GroupStatus.OPEN
    assert m.status == MembershipStatus.LEFT
    # после выхода можно подать заявку снова
    await svc.apply_to_group(session_maker, group.id, u1.id)


async def test_leave_pending_does_not_touch_slots(session_maker) -> None:
    _, _, group, (u1, _) = await make_world(session_maker, free=2)
    await svc.apply_to_group(session_maker, group.id, u1.id)
    dep = await svc.leave_group(session_maker, group.id, u1.id)
    assert not dep.was_approved
    assert (await reload(session_maker, Group, group.id)).slots_free == 2


async def test_leave_when_not_member(session_maker) -> None:
    _, _, group, (u1, _) = await make_world(session_maker)
    with pytest.raises(groups_svc.GroupError, match="не состоите"):
        await svc.leave_group(session_maker, group.id, u1.id)


async def test_close_group(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await svc.apply_to_group(session_maker, group.id, u1.id)
    await svc.decide_application(session_maker, a1.membership.id, owner.id, True)
    a2 = await svc.apply_to_group(session_maker, group.id, u2.id)
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


async def test_group_view_hides_contact_until_approved(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    pending = await svc.get_group_view(session_maker, group.id, u1.id)
    assert pending.role == "member" and pending.owner_contact is None
    await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    approved = await svc.get_group_view(session_maker, group.id, u1.id)
    assert approved.owner_contact.id == owner.id
    assert (await svc.get_group_view(session_maker, group.id, owner.id)).role == "owner"
    assert await svc.get_group_view(session_maker, group.id, u2.id) is None


async def test_members_list_owner_only(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    app = await svc.apply_to_group(session_maker, group.id, u1.id)
    await svc.apply_to_group(session_maker, group.id, u2.id)
    await svc.decide_application(session_maker, app.membership.id, owner.id, True)
    members = await svc.list_members(session_maker, group.id, owner.id)
    assert [m.id for m in members] == [u1.id]
    with pytest.raises(groups_svc.GroupError):
        await svc.list_members(session_maker, group.id, u1.id)


async def test_user_groups_cover_owner_and_member_roles(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=2)
    await svc.apply_to_group(session_maker, group.id, u1.id)
    mine = await svc.list_user_groups(session_maker, owner.id)
    theirs = await svc.list_user_groups(session_maker, u1.id)
    assert [e.role for e in mine] == ["owner"]
    assert [(e.role, e.membership_status) for e in theirs] == [("member", MembershipStatus.PENDING)]
