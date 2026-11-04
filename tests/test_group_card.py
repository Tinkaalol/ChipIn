from datetime import date

from sqlalchemy import select

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus
from bot.handlers.my_groups import build_card
from bot.services import groups as groups_svc
from bot.services import memberships as ms
from bot.services.groups import Role
from tests.fakes import callback_update, make_bot
from tests.test_memberships import make_world

TODAY = date(2026, 6, 3)  # день оплаты в make_world — 10-е


async def world_with_roles(sm):
    """Организатор, принятый участник, ждущий заявку и посторонний."""
    owner, _, group, (member, pending, stranger) = await make_world(
        sm, free=2, total=4, applicants=3
    )
    app = await groups_svc.request_join(sm, group.id, member.id)
    await groups_svc.approve_member(sm, app.membership.id, owner.id)
    await groups_svc.request_join(sm, group.id, pending.id)
    return owner, group, member, pending, stranger


async def test_get_user_role_for_every_case(session_maker) -> None:
    owner, group, member, pending, stranger = await world_with_roles(session_maker)
    role = groups_svc.get_user_role
    assert await role(session_maker, group.id, owner.id) == Role.OWNER
    assert await role(session_maker, group.id, member.id) == Role.MEMBER
    assert await role(session_maker, group.id, pending.id) == Role.PENDING
    assert await role(session_maker, group.id, stranger.id) == Role.NONE
    assert await role(session_maker, 9999, owner.id) == Role.NONE  # группы нет


async def test_role_after_reject_and_leave_is_none(session_maker) -> None:
    owner, group, member, pending, _ = await world_with_roles(session_maker)
    async with session_maker() as s:
        m = await s.scalar(select(Membership).where(Membership.user_id == pending.id))
    await groups_svc.reject_member(session_maker, m.id, owner.id)
    await ms.leave_group(session_maker, group.id, member.id)
    assert await groups_svc.get_user_role(session_maker, group.id, pending.id) == Role.NONE
    assert await groups_svc.get_user_role(session_maker, group.id, member.id) == Role.NONE


async def test_card_data_depends_on_role(session_maker) -> None:
    owner, group, member, pending, stranger = await world_with_roles(session_maker)
    card = ms.get_group_card

    owner_card = await card(session_maker, group.id, owner.id)
    assert owner_card.role == Role.OWNER
    assert [u.id for u in owner_card.participants] == [member.id]
    assert [a.user.id for a in owner_card.applicants] == [pending.id]

    member_card = await card(session_maker, group.id, member.id)
    assert member_card.owner.id == owner.id and member_card.applicants == []
    assert [u.id for u in member_card.participants] == [member.id]

    pending_card = await card(session_maker, group.id, pending.id)
    assert pending_card.role == Role.PENDING
    assert pending_card.owner is None and pending_card.participants == []

    stranger_card = await card(session_maker, group.id, stranger.id)
    assert stranger_card.role == Role.NONE
    assert stranger_card.owner is None
    assert stranger_card.participants == [] and stranger_card.applicants == []


async def test_stranger_gets_no_card_for_unavailable_groups(session_maker) -> None:
    owner, group, member, _, stranger = await world_with_roles(session_maker)
    async with session_maker() as s:
        g = await s.get(Group, group.id)
        g.status, g.slots_free = GroupStatus.FULL, 0
        await s.commit()
    assert await ms.get_group_card(session_maker, group.id, stranger.id) is None
    assert await ms.get_group_card(session_maker, group.id, member.id) is not None  # свои видны
    assert await ms.get_group_card(session_maker, 9999, owner.id) is None
    async with session_maker() as s:
        (await s.get(Group, group.id)).status = GroupStatus.CLOSED
        await s.commit()
    assert await ms.get_group_card(session_maker, group.id, owner.id) is None


def texts_and_buttons(card, page=None):
    text, rows = build_card(card, page, TODAY)
    return text, [(t, d) for row in rows for t, d in row]


async def test_card_for_stranger(session_maker) -> None:
    _, group, _, _, stranger = await world_with_roles(session_maker)
    card = await ms.get_group_card(session_maker, group.id, stranger.id)
    text, buttons = texts_and_buttons(card, page=2)
    assert "300.00 ₸" in text  # подписка целиком
    assert "100.00 ₸ в месяц" in text  # взнос на человека
    # в группе на 4 места: организатор, принятый участник и ещё одно свободное
    assert "Участников: 3 из 4" in text and "свободно мест: 1" in text
    assert "10-го числа, ближайшая — 10.06.2026" in text and "идёт набор" in text
    assert ("Подать заявку", f"s:apply:{group.id}") in buttons
    assert ("« Назад к списку", f"s:svc:{card.service.id}:2") in buttons
    assert "owner" not in text and "Организатор:" not in text
    assert not [t for t, _ in buttons if "Выйти" in t or "Участники" in t]


async def test_card_for_pending(session_maker) -> None:
    _, group, _, pending, _ = await world_with_roles(session_maker)
    text, buttons = texts_and_buttons(await ms.get_group_card(session_maker, group.id, pending.id))
    assert "Заявка отправлена, ждём ответа" in text
    assert ("Отозвать заявку", f"g:leave:{group.id}") in buttons
    assert "Организатор:" not in text and "Подать заявку" not in [t for t, _ in buttons]


async def test_card_for_member_shows_owner_and_participants(session_maker) -> None:
    owner, group, member, _, _ = await world_with_roles(session_maker)
    text, buttons = texts_and_buttons(await ms.get_group_card(session_maker, group.id, member.id))
    assert "Организатор: @owner" in text
    assert "• @u0" in text  # участник виден по username
    assert str(member.telegram_id) not in text  # лишних данных нет
    assert ("Выйти из группы", f"g:leave:{group.id}") in buttons
    assert ("« Назад", "menu:my") in buttons


async def test_card_for_owner_shows_applications_with_actions(session_maker) -> None:
    owner, group, _, pending, _ = await world_with_roles(session_maker)
    card = await ms.get_group_card(session_maker, group.id, owner.id)
    text, buttons = texts_and_buttons(card)
    assert "Вы — организатор" in text and "Заявки ждут ответа:\n• @u1" in text
    mid = card.applicants[0].membership_id
    assert ("✅ Принять @u1", f"m:ok:{mid}") in buttons
    assert ("❌ Отклонить", f"m:no:{mid}") in buttons
    assert ("Участники", f"g:members:{group.id}") in buttons
    assert ("Закрыть группу", f"g:close:{group.id}") in buttons


def test_next_payment_in_card_rolls_to_next_month() -> None:
    from bot.services.reminders import next_payment_date

    assert next_payment_date(10, date(2026, 6, 11)) == date(2026, 7, 10)


async def test_forged_group_id_reveals_nothing(dp, session_maker) -> None:
    owner, group, member, pending, stranger = await world_with_roles(session_maker)
    bot, session = make_bot()
    # посторонний подставляет чужой group_id в закрытые и открытые экраны
    for data in (f"g:view:{group.id}", f"g:members:{group.id}", f"g:close:{group.id}"):
        await dp.feed_update(bot, callback_update(stranger.telegram_id, data, "stranger"))
    shown = "\n".join(session.texts())
    assert "@u0" not in shown and "@owner" not in shown  # участники и организатор не раскрыты
    assert "Контакт организатора откроется" in shown  # публичный вид, как у любого соискателя
    async with session_maker() as s:
        assert (await s.get(Group, group.id)).status == GroupStatus.OPEN


async def test_member_cannot_open_foreign_group_details(dp, session_maker) -> None:
    owner, group, member, _, _ = await world_with_roles(session_maker)
    other = await _other_group(session_maker, owner)
    bot, session = make_bot()
    await dp.feed_update(bot, callback_update(member.telegram_id, f"g:view:{other.id}", "u0"))
    assert "Контакт организатора откроется" in session.texts()[-1]
    assert "Участники" not in session.texts()[-1].split("Контакт")[0]


async def _other_group(sm, owner):
    from decimal import Decimal

    async with sm() as s:
        g = Group(
            service_id=1,
            owner_id=owner.id,
            price_per_member=Decimal("50"),
            slots_total=3,
            slots_free=2,
            billing_day=4,
        )
        s.add(g)
        await s.commit()
        return g


async def test_card_buttons_work_end_to_end(dp, session_maker) -> None:
    owner, group, member, pending, _ = await world_with_roles(session_maker)
    bot, session = make_bot()
    await dp.feed_update(bot, callback_update(owner.telegram_id, f"g:view:{group.id}", "owner"))
    assert "Заявки ждут ответа" in session.texts_to(owner.telegram_id)[-1]
    mid = (await ms.get_group_card(session_maker, group.id, owner.id)).applicants[0].membership_id
    await dp.feed_update(bot, callback_update(owner.telegram_id, f"m:ok:{mid}", "owner"))
    async with session_maker() as s:
        assert (await s.get(Membership, mid)).status == MembershipStatus.APPROVED
    assert any("принята" in t for t in session.texts_to(pending.telegram_id))
    await dp.feed_update(bot, callback_update(pending.telegram_id, f"g:view:{group.id}", "u1"))
    assert "Организатор: @owner" in session.texts_to(pending.telegram_id)[-1]
