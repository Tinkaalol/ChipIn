import pytest
from sqlalchemy import select

from shared.db.models import (
    Group,
    GroupStatus,
    Membership,
    MembershipStatus,
    Report,
    ReportStatus,
    Service,
    User,
)
from shared.services import groups as groups_svc
from shared.services import moderation as mod
from tests.test_memberships import make_world, reload


async def test_find_user_by_username_is_case_and_at_insensitive(session_maker) -> None:
    owner, *_ = await make_world(session_maker)
    assert (await mod.find_user_by_username(session_maker, "@Owner")).id == owner.id
    with pytest.raises(groups_svc.GroupError, match="не найден"):
        await mod.find_user_by_username(session_maker, "@nobody")
    with pytest.raises(groups_svc.GroupError, match="Укажите"):
        await mod.find_user_by_username(session_maker, "  ")


async def test_ban_closes_owned_groups_and_rejects_pending(session_maker) -> None:
    owner, _, group, (u1, u2) = await make_world(session_maker, free=2)
    a1 = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, a1.membership.id, owner.id)
    a2 = await groups_svc.request_join(session_maker, group.id, u2.id)

    result = await mod.ban_user(session_maker, owner.id, protected_ids=set())

    assert (await reload(session_maker, User, owner.id)).is_banned
    assert (await reload(session_maker, Group, group.id)).status == GroupStatus.CLOSED
    assert (await reload(session_maker, Membership, a2.membership.id)).status == (
        MembershipStatus.REJECTED
    )
    assert len(result.closed) == 1
    assert sorted(result.closed[0].notify_ids) == [u1.telegram_id, u2.telegram_id]


async def test_ban_removes_member_and_returns_slot(session_maker) -> None:
    owner, _, group, (u1, _) = await make_world(session_maker, free=1)
    app = await groups_svc.request_join(session_maker, group.id, u1.id)
    await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    assert (await reload(session_maker, Group, group.id)).status == GroupStatus.FULL

    result = await mod.ban_user(session_maker, u1.id, protected_ids=set())

    g = await reload(session_maker, Group, group.id)
    assert g.slots_free == 1 and g.status == GroupStatus.OPEN
    assert (await reload(session_maker, Membership, app.membership.id)).status == (
        MembershipStatus.LEFT
    )
    assert [left.owner_telegram_id for left in result.left] == [owner.telegram_id]


async def test_cannot_ban_admin_or_twice_or_missing(session_maker) -> None:
    owner, _, _, (u1, _) = await make_world(session_maker)
    with pytest.raises(groups_svc.GroupError, match="Администратора"):
        await mod.ban_user(session_maker, owner.id, protected_ids={owner.telegram_id})
    assert not (await reload(session_maker, User, owner.id)).is_banned
    await mod.ban_user(session_maker, u1.id, protected_ids=set())
    with pytest.raises(mod.AlreadyBannedError):
        await mod.ban_user(session_maker, u1.id, protected_ids=set())
    with pytest.raises(groups_svc.GroupError, match="не найден"):
        await mod.ban_user(session_maker, 999, protected_ids=set())


async def test_banned_user_cannot_apply_and_unban_restores(session_maker) -> None:
    _, _, group, (u1, _) = await make_world(session_maker, free=2)
    await mod.ban_user(session_maker, u1.id, protected_ids=set())
    assert await mod.is_banned(session_maker, u1.telegram_id)
    with pytest.raises(groups_svc.GroupError, match="недоступна"):
        await groups_svc.request_join(session_maker, group.id, u1.id)
    await mod.unban_user(session_maker, u1.id)
    assert not await mod.is_banned(session_maker, u1.telegram_id)
    await groups_svc.request_join(session_maker, group.id, u1.id)
    with pytest.raises(groups_svc.GroupError, match="не забанен"):
        await mod.unban_user(session_maker, u1.id)


async def test_closed_group_is_not_in_search_after_ban(session_maker) -> None:
    owner, service, _, (u1, _) = await make_world(session_maker, free=2)
    await mod.ban_user(session_maker, owner.id, protected_ids=set())
    assert await groups_svc.list_open_groups(session_maker, service.id, u1.id, 0, 5) == []


async def _joined_world(sm):
    owner, service, group, (u1, u2) = await make_world(sm, free=2)
    app = await groups_svc.request_join(sm, group.id, u1.id)
    await groups_svc.approve_member(sm, app.membership.id, owner.id)
    return owner, service, group, u1, u2


async def test_report_created_for_participants(session_maker) -> None:
    owner, _, group, u1, _ = await _joined_world(session_maker)
    report = await mod.create_report(session_maker, u1.id, group.id, owner.id, "unpaid", " поздно ")
    assert report.status == ReportStatus.NEW
    assert report.reason == "Не платит: поздно"
    # организатор жалуется на участника
    await mod.create_report(session_maker, owner.id, group.id, u1.id, "gone", "")


async def test_report_validation(session_maker) -> None:
    owner, _, group, u1, u2 = await _joined_world(session_maker)
    create = mod.create_report
    with pytest.raises(groups_svc.GroupError, match="себя"):
        await create(session_maker, u1.id, group.id, u1.id, "gone", "")
    with pytest.raises(groups_svc.GroupError, match="причину"):
        await create(session_maker, u1.id, group.id, owner.id, "spam", "")
    with pytest.raises(groups_svc.GroupError, match="опишите"):
        await create(session_maker, u1.id, group.id, owner.id, "other", "  ")
    with pytest.raises(groups_svc.GroupError, match="длинный"):
        await create(session_maker, u1.id, group.id, owner.id, "gone", "x" * 501)
    with pytest.raises(groups_svc.GroupError, match="своей группы"):
        await create(session_maker, u2.id, group.id, owner.id, "gone", "")  # u2 не участник
    with pytest.raises(groups_svc.GroupError, match="не найдена"):
        await create(session_maker, u1.id, 999, owner.id, "gone", "")
    await create(session_maker, u1.id, group.id, owner.id, "gone", "")
    with pytest.raises(groups_svc.GroupError, match="уже отправляли"):
        await create(session_maker, u1.id, group.id, owner.id, "fraud", "")


async def test_resolve_report_with_ban(session_maker) -> None:
    owner, _, group, u1, _ = await _joined_world(session_maker)
    report = await mod.create_report(session_maker, owner.id, group.id, u1.id, "unpaid", "")
    views = await mod.list_open_reports(session_maker)
    assert [v.report.id for v in views] == [report.id]
    assert views[0].target.id == u1.id and views[0].service.name == "Spotify"

    outcome = await mod.resolve_report(session_maker, report.id, True, protected_ids=set())

    assert outcome.report.status == ReportStatus.RESOLVED
    assert outcome.ban is not None and outcome.ban.user.id == u1.id
    assert await mod.is_banned(session_maker, u1.telegram_id)
    assert await mod.list_open_reports(session_maker) == []
    with pytest.raises(groups_svc.GroupError, match="уже рассмотрена"):
        await mod.resolve_report(session_maker, report.id, True, protected_ids=set())


async def test_resolve_report_dismiss_does_not_ban(session_maker) -> None:
    owner, _, group, u1, _ = await _joined_world(session_maker)
    report = await mod.create_report(session_maker, u1.id, group.id, owner.id, "gone", "")
    outcome = await mod.resolve_report(session_maker, report.id, False, protected_ids=set())
    assert outcome.report.status == ReportStatus.DISMISSED and outcome.ban is None
    assert not await mod.is_banned(session_maker, owner.telegram_id)


async def test_resolve_report_on_admin_keeps_report_open(session_maker) -> None:
    owner, _, group, u1, _ = await _joined_world(session_maker)
    report = await mod.create_report(session_maker, u1.id, group.id, owner.id, "fraud", "")
    with pytest.raises(groups_svc.GroupError, match="Администратора"):
        await mod.resolve_report(session_maker, report.id, True, protected_ids={owner.telegram_id})
    async with session_maker() as s:
        assert (await s.get(Report, report.id)).status == ReportStatus.NEW


async def test_resolve_report_already_banned_target_still_closes(session_maker) -> None:
    owner, _, group, u1, _ = await _joined_world(session_maker)
    report = await mod.create_report(session_maker, owner.id, group.id, u1.id, "gone", "")
    await mod.ban_user(session_maker, u1.id, protected_ids=set())
    outcome = await mod.resolve_report(session_maker, report.id, True, protected_ids=set())
    assert outcome.report.status == ReportStatus.RESOLVED and outcome.ban is None
    with pytest.raises(groups_svc.GroupError, match="не найдена"):
        await mod.resolve_report(session_maker, 999, True, protected_ids=set())
    assert (await mod.list_open_reports(session_maker)) == []
    async with session_maker() as s:
        assert (await s.scalars(select(Service))).first() is not None
