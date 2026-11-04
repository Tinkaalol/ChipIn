from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User
from bot.services import groups as svc
from bot.services.catalog import seed_services
from tests.fakes import callback_update, make_bot
from tests.test_memberships import make_world

VIEWER = 300


async def add_group(
    sm,
    owner_id: int,
    service_id: int,
    free: int = 2,
    total: int = 4,
    status: GroupStatus = GroupStatus.OPEN,
    age_minutes: int = 0,
) -> Group:
    async with sm() as s:
        group = Group(
            service_id=service_id,
            owner_id=owner_id,
            price_per_member=Decimal("100"),
            slots_total=total,
            slots_free=free,
            billing_day=5,
            status=status,
            created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        )
        s.add(group)
        await s.commit()
        return group


async def viewer_and_world(sm):
    owner, service, base_group, (viewer, _) = await make_world(sm, free=1, total=3)
    return owner, service, base_group, viewer


async def ids(sm, service_id: int, viewer_id: int, offset: int = 0, limit: int = 5) -> list[int]:
    return [g.id for g in await svc.list_open_groups(sm, service_id, viewer_id, offset, limit)]


async def test_only_open_groups_with_free_slots(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)
    await add_group(session_maker, owner.id, service.id, free=0, status=GroupStatus.FULL)
    await add_group(session_maker, owner.id, service.id, free=1, status=GroupStatus.CLOSED)
    await add_group(session_maker, owner.id, service.id, free=0)  # open, но мест нет
    assert await ids(session_maker, service.id, viewer.id) == [base.id]


async def test_excludes_own_joined_pending_and_rejected(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)
    other = await add_group(session_maker, owner.id, service.id)
    pending = await add_group(session_maker, owner.id, service.id)
    approved = await add_group(session_maker, owner.id, service.id)
    rejected = await add_group(session_maker, owner.id, service.id)
    left = await add_group(session_maker, owner.id, service.id)
    async with session_maker() as s:
        for group, status in (
            (pending, MembershipStatus.PENDING),
            (approved, MembershipStatus.APPROVED),
            (rejected, MembershipStatus.REJECTED),
            (left, MembershipStatus.LEFT),
        ):
            s.add(
                Membership(
                    group_id=group.id,
                    user_id=viewer.id,
                    status=status,
                    decided_at=datetime.now(UTC),
                )
            )
        await s.commit()

    visible = set(await ids(session_maker, service.id, viewer.id))
    assert visible == {base.id, other.id, left.id}  # вышедший может вернуться
    assert await ids(session_maker, service.id, owner.id) == []  # свои группы не видны


async def test_sorted_by_free_slots_then_newest(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)  # base: 1 свободное
    few_new = await add_group(session_maker, owner.id, service.id, free=1, age_minutes=1)
    many_old = await add_group(session_maker, owner.id, service.id, free=3, age_minutes=100)
    many_new = await add_group(session_maker, owner.id, service.id, free=3, age_minutes=10)
    mid = await add_group(session_maker, owner.id, service.id, free=2, age_minutes=50)
    order = await ids(session_maker, service.id, viewer.id)
    # base создана только что, поэтому среди групп с одним местом она новее few_new
    assert order == [many_new.id, many_old.id, mid.id, base.id, few_new.id]


async def test_offset_limit_and_filter_by_service(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)
    async with session_maker() as s:
        other_service = Service(
            name="Other", plan_name="Plan", price_month=Decimal("10"), max_members=4
        )
        s.add(other_service)
        await s.commit()
    for _ in range(6):
        await add_group(session_maker, owner.id, service.id)
    foreign = await add_group(session_maker, owner.id, other_service.id)
    everything = await ids(session_maker, service.id, viewer.id, 0, 100)
    assert len(everything) == 7 and foreign.id not in everything
    assert await ids(session_maker, service.id, viewer.id, 0, 5) == everything[:5]
    assert await ids(session_maker, service.id, viewer.id, 5, 5) == everything[5:]
    assert await ids(session_maker, service.id, viewer.id, 50, 5) == []
    assert await ids(session_maker, service.id, viewer.id, -3, 2) == everything[:2]


async def test_counts_by_service_use_same_rules(session_maker) -> None:
    owner, service, _, viewer = await viewer_and_world(session_maker)
    await add_group(session_maker, owner.id, service.id)
    await add_group(session_maker, owner.id, service.id, free=0)
    counts = await svc.count_open_groups_by_service(session_maker, viewer.id)
    assert counts == {service.id: 2}
    assert await svc.count_open_groups_by_service(session_maker, owner.id) == {}


async def test_applying_hides_group_from_list(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)
    assert await ids(session_maker, service.id, viewer.id) == [base.id]
    await svc.request_join(session_maker, base.id, viewer.id)
    assert await ids(session_maker, service.id, viewer.id) == []


def buttons(session) -> list[tuple[str, str]]:
    last = [c for c in session.calls if getattr(c, "reply_markup", None)][-1]
    return [(b.text, b.callback_data) for row in last.reply_markup.inline_keyboard for b in row]


async def test_service_list_shows_group_counts(dp, session_maker) -> None:
    await seed_services(session_maker)
    async with session_maker() as s:
        owner = User(telegram_id=1, username="o")
        s.add(owner)
        await s.commit()
    for _ in range(2):
        await add_group(session_maker, owner.id, 1)
    bot, session = make_bot()
    await dp.feed_update(bot, callback_update(VIEWER, "menu:find"))
    labels = [text for text, _ in buttons(session)]
    assert "Spotify Family (2)" in labels
    assert "YouTube Premium Family (0)" in labels


async def test_empty_list_offers_to_create_group(dp, session_maker) -> None:
    await seed_services(session_maker)
    bot, session = make_bot()
    await dp.feed_update(bot, callback_update(VIEWER, "s:svc:1:0"))
    assert session.texts()[-1] == "Пока нет групп"
    assert ("Создать свою группу", "menu:create") in buttons(session)


async def test_pager_and_card_hide_organizer(dp, session_maker) -> None:
    await seed_services(session_maker)
    async with session_maker() as s:
        owner = User(telegram_id=1, username="secret_owner")
        s.add(owner)
        await s.commit()
    created = [await add_group(session_maker, owner.id, 1, total=4) for _ in range(7)]
    bot, session = make_bot()

    await dp.feed_update(bot, callback_update(VIEWER, "s:svc:1:0"))
    first = buttons(session)
    assert [t for t, _ in first if t.startswith("Далее")] == ["Далее ▶"]
    assert not [t for t, _ in first if t.startswith("◀")]
    items = [t for t, d in first if d.startswith("s:grp:")]
    assert len(items) == 5
    assert items[0] == "100.00 ₸ · 2 из 4 мест · оплата 5-го"

    await dp.feed_update(bot, callback_update(VIEWER, "s:svc:1:1"))
    second = buttons(session)
    assert len([d for _, d in second if d.startswith("s:grp:")]) == 2
    assert ("◀ Назад", "s:svc:1:0") in second and not [t for t, _ in second if "Далее" in t]

    await dp.feed_update(bot, callback_update(VIEWER, f"s:grp:{created[0].id}:1"))
    card = session.texts()[-1]
    assert (
        "secret_owner" not in card and "свободно мест: 2" in card and "Участников: 2 из 4" in card
    )
    everything = "\n".join(session.texts())
    assert "secret_owner" not in everything


async def test_rejected_group_returns_to_list_after_24_hours(session_maker) -> None:
    owner, service, base, viewer = await viewer_and_world(session_maker)
    old = await add_group(session_maker, owner.id, service.id)
    async with session_maker() as s:
        s.add(
            Membership(
                group_id=base.id,
                user_id=viewer.id,
                status=MembershipStatus.REJECTED,
                decided_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        s.add(
            Membership(
                group_id=old.id,
                user_id=viewer.id,
                status=MembershipStatus.REJECTED,
                decided_at=datetime.now(UTC) - timedelta(hours=25),
            )
        )
        await s.commit()
    assert await ids(session_maker, service.id, viewer.id) == [old.id]
