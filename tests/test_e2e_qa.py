"""Сквозной сценарий, проверки подмены callback_data, приватности и инвариантов мест."""

import asyncio
import random
from datetime import datetime

import pytest
from sqlalchemy import func, select

from bot.db.models import (
    Group,
    GroupStatus,
    Membership,
    MembershipStatus,
    Report,
    Service,
    User,
)
from bot.services import groups as groups_svc
from bot.services import memberships as ms
from bot.services import moderation as mod
from bot.services.catalog import seed_services
from bot.services.reminders import ALMATY, next_payment_date
from tests.fakes import FakeSession, callback_update, make_bot, message_update

ALICE, BOB, CAROL = 1001, 1002, 1003  # организатор и двое участников (telegram_id)


def markup_of(session: FakeSession, chat_id: int):
    """Клавиатура последнего сообщения, адресованного пользователю."""
    sent = [
        c
        for c in session.calls
        if getattr(c, "chat_id", None) == chat_id and getattr(c, "reply_markup", None)
    ]
    return sent[-1].reply_markup if sent else None


def buttons_of(session: FakeSession, chat_id: int) -> list[tuple[str, str]]:
    markup = markup_of(session, chat_id)
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


class Driver:
    def __init__(self, dp, bot, session) -> None:
        self.dp, self.bot, self.session = dp, bot, session

    async def press(self, user: int, data: str, name: str) -> None:
        await self.dp.feed_update(self.bot, callback_update(user, data, name))

    async def say(self, user: int, text: str, name: str) -> None:
        await self.dp.feed_update(self.bot, message_update(user, text, name))

    def last(self, user: int) -> str:
        return self.session.texts_to(user)[-1]

    def find_button(self, user: int, starts_with: str) -> str:
        for _, data in buttons_of(self.session, user):
            if data.startswith(starts_with):
                return data
        raise AssertionError(
            f"у {user} нет кнопки {starts_with!r}: {buttons_of(self.session, user)}"
        )


async def group_state(sm, group_id: int) -> tuple[int, GroupStatus]:
    async with sm() as s:
        g = await s.get(Group, group_id)
        return g.slots_free, g.status


async def test_end_to_end_scenario(dp, session_maker) -> None:
    await seed_services(session_maker)
    bot, session = make_bot()
    d = Driver(dp, bot, session)

    # 1. Алиса создаёт группу на 2 места: она сама и один участник
    for data in ("menu:create", "cg:svc:1", "cg:next", "cg:total:2", "cg:free:1", "cg:day:7"):
        await d.press(ALICE, data, "alice")
    assert "Проверьте группу" in d.last(ALICE)
    await d.press(ALICE, "cg:confirm", "alice")
    async with session_maker() as s:
        group = await s.scalar(select(Group))
        alice = await s.scalar(select(User).where(User.telegram_id == ALICE))
    assert group.status == GroupStatus.OPEN and group.slots_free == 1

    # 2. Боб находит группу: видит число групп, список и карточку без контакта
    await d.press(BOB, "menu:find", "bob")
    assert "Spotify Family (1)" in [t for t, _ in buttons_of(session, BOB)]
    await d.press(BOB, "s:svc:1:0", "bob")
    item = d.find_button(BOB, "s:grp:")
    await d.press(BOB, item, "bob")
    card = d.last(BOB)
    assert "Контакт организатора откроется" in card and "alice" not in card.lower()
    assert ("Подать заявку", f"s:apply:{group.id}") in buttons_of(session, BOB)

    # 3. Боб подаёт заявку, у Алисы приходит сообщение с кнопками
    await d.press(BOB, f"s:apply:{group.id}", "bob")
    assert "Заявка отправлена" in d.last(BOB)
    assert d.last(ALICE).startswith("Новая заявка в группу Spotify Family: @bob")
    accept = d.find_button(ALICE, "m:ok:")
    assert d.find_button(ALICE, "m:no:").startswith("m:no:")
    await d.press(BOB, f"g:view:{group.id}", "bob")
    assert "Заявка отправлена, ждём ответа" in d.last(BOB)
    assert "alice" not in "\n".join(session.texts_to(BOB)).lower()  # контакта всё ещё нет

    # 4. Алиса принимает: группа заполнена, Бобу приходит контакт и дата оплаты
    await d.press(ALICE, accept, "alice")
    assert await group_state(session_maker, group.id) == (0, GroupStatus.FULL)
    welcome = d.last(BOB)
    expected_date = next_payment_date(7, datetime.now(ALMATY).date())
    assert "@alice" in welcome and f"{expected_date:%d.%m.%Y}" in welcome

    # 5. Боб открывает карточку: контакт и список участников
    await d.press(BOB, f"g:view:{group.id}", "bob")
    member_card = d.last(BOB)
    assert "Организатор: @alice" in member_card and "• @bob" in member_card
    assert ("Выйти из группы", f"g:leave:{group.id}") in buttons_of(session, BOB)
    # группа заполнена, Кэрол в поиске её не видит
    await d.press(CAROL, "menu:find", "carol")
    assert "Spotify Family (0)" in [t for t, _ in buttons_of(session, CAROL)]

    # 6. Боб выходит: Алиса снова видит свободное место, группа снова в поиске
    await d.press(BOB, f"g:leave:{group.id}", "bob")
    assert "Точно выйти" in d.last(BOB)
    await d.press(BOB, f"g:leaveok:{group.id}", "bob")
    assert "Вы вышли" in d.last(BOB)
    assert "вышел из группы" in d.last(ALICE)
    assert await group_state(session_maker, group.id) == (1, GroupStatus.OPEN)
    await d.press(ALICE, f"g:view:{group.id}", "alice")
    assert "свободно мест: 1" in d.last(ALICE) and "идёт набор" in d.last(ALICE)
    await d.press(BOB, f"g:view:{group.id}", "bob")  # Боб больше не участник
    assert "Организатор: @alice" not in d.last(BOB)
    await d.press(CAROL, "menu:find", "carol")
    assert "Spotify Family (1)" in [t for t, _ in buttons_of(session, CAROL)]

    # 7. Кэрол подаёт заявку, Алиса принимает, затем удаляет её
    await d.press(CAROL, f"s:apply:{group.id}", "carol")
    await d.press(ALICE, d.find_button(ALICE, "m:ok:"), "alice")
    assert await group_state(session_maker, group.id) == (0, GroupStatus.FULL)
    await d.press(ALICE, f"g:members:{group.id}", "alice")
    assert "@carol" in d.last(ALICE)
    remove = d.find_button(ALICE, "g:rm:")
    await d.press(ALICE, remove, "alice")
    assert d.last(ALICE) == "Удалить @carol из группы? Он получит уведомление."
    await d.press(ALICE, remove.replace("g:rm:", "g:rmok:"), "alice")
    assert d.session.texts_to(CAROL)[-1] == "Организатор удалил вас из группы Spotify Family."
    await d.say(ALICE, "Не платит", "alice")
    assert "Не платит" in d.last(CAROL)
    assert await group_state(session_maker, group.id) == (1, GroupStatus.OPEN)

    # 8. Кэрол не может сразу вернуться, а Боб (вышел сам) может
    await d.press(CAROL, f"s:apply:{group.id}", "carol")
    alerts = [c.text for c in session.calls if type(c).__name__ == "AnswerCallbackQuery" and c.text]
    assert any("через 7 дн" in a for a in alerts)
    await d.press(BOB, f"s:apply:{group.id}", "bob")
    assert "Заявка отправлена" in d.last(BOB)

    # 9. Приватность за весь сценарий: Боб и Кэрол не видели контакт Алисы,
    #    пока не были приняты (проверяем всё, что получала Кэрол до одобрения)
    carol_texts = session.texts_to(CAROL)
    first_welcome = next(i for i, t in enumerate(carol_texts) if "принята" in t)
    before_approval = "\n".join(carol_texts[:first_welcome])
    assert "alice" not in before_approval.lower() and str(ALICE) not in before_approval
    assert alice.telegram_id == ALICE
    await check_invariants(session_maker, {group.id: 1})


# ---- инварианты мест


async def check_invariants(sm, initial_free: dict[int, int]) -> None:
    """slots_free = исходные свободные − принятые; статус группы согласован с числом мест."""
    async with sm() as s:
        for g in (await s.scalars(select(Group))).all():
            approved = await s.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.group_id == g.id, Membership.status == MembershipStatus.APPROVED)
            )
            assert 0 <= g.slots_free <= g.slots_total - 1, (g.id, g.slots_free)
            assert g.slots_free == initial_free[g.id] - approved, (g.id, g.slots_free, approved)
            if g.status != GroupStatus.CLOSED:
                assert (g.status == GroupStatus.FULL) == (g.slots_free == 0), (
                    g.status,
                    g.slots_free,
                )
            active = await s.execute(
                select(Membership.user_id, func.count())
                .where(
                    Membership.group_id == g.id,
                    Membership.status.in_((MembershipStatus.PENDING, MembershipStatus.APPROVED)),
                )
                .group_by(Membership.user_id)
            )
            assert all(count == 1 for _, count in active), "двойное активное участие"


async def make_arena(sm, users: int = 7, total: int = 5, free: int = 3):
    from decimal import Decimal

    async with sm() as s:
        owner = User(telegram_id=1, username="owner")
        service = Service(name="S", plan_name="P", price_month=Decimal("100"), max_members=6)
        people = [User(telegram_id=10 + i, username=f"u{i}") for i in range(users)]
        s.add_all([owner, service, *people])
        await s.flush()
        group = Group(
            service_id=service.id,
            owner_id=owner.id,
            price_per_member=Decimal("20"),
            slots_total=total,
            slots_free=free,
            billing_day=5,
        )
        s.add(group)
        await s.commit()
        return owner, group, people


async def random_op(sm, rng: random.Random, owner, group, people) -> None:
    async with sm() as s:
        pending = list(
            await s.scalars(
                select(Membership.id).where(Membership.status == MembershipStatus.PENDING)
            )
        )
        approved = list(
            await s.scalars(
                select(Membership.id).where(Membership.status == MembershipStatus.APPROVED)
            )
        )
    person = rng.choice(people)
    op = rng.choice(["join", "join", "approve", "approve", "reject", "leave", "remove", "ban"])
    if op == "join":
        await groups_svc.request_join(sm, group.id, person.id)
    elif op == "approve" and pending:
        await groups_svc.approve_member(sm, rng.choice(pending), owner.id)
    elif op == "reject" and pending:
        await groups_svc.reject_member(sm, rng.choice(pending), owner.id)
    elif op == "leave":
        await ms.leave_group(sm, group.id, person.id)
    elif op == "remove" and approved:
        await ms.remove_member(sm, rng.choice(approved), owner.id)
    elif op == "ban":
        await mod.ban_user(sm, person.id, set())
        await mod.unban_user(sm, person.id)  # возвращаем, чтобы продолжать сценарий


@pytest.mark.parametrize("seed", range(8))
async def test_slots_stay_consistent_after_random_sequences(session_maker, seed: int) -> None:
    rng = random.Random(seed)
    owner, group, people = await make_arena(session_maker)
    for _ in range(150):
        try:
            await random_op(session_maker, rng, owner, group, people)
        except groups_svc.GroupError:
            pass  # отказ по правилам — штатная ситуация
        await check_invariants(session_maker, {group.id: group.slots_free})


async def test_slots_stay_consistent_under_concurrent_operations(file_session_maker) -> None:
    sm = file_session_maker
    rng = random.Random(7)
    owner, group, people = await make_arena(sm, users=8)
    initial = {group.id: group.slots_free}
    for _ in range(25):
        results = await asyncio.gather(
            *[random_op(sm, rng, owner, group, people) for _ in range(6)],
            return_exceptions=True,
        )
        unexpected = [
            r
            for r in results
            if isinstance(r, Exception) and not isinstance(r, groups_svc.GroupError)
        ]
        assert not unexpected, unexpected
        await check_invariants(sm, initial)


async def test_closing_and_banning_keep_slot_arithmetic(session_maker) -> None:
    owner, group, people = await make_arena(session_maker)
    initial = {group.id: group.slots_free}
    for person in people[:3]:
        app = await groups_svc.request_join(session_maker, group.id, person.id)
        await groups_svc.approve_member(session_maker, app.membership.id, owner.id)
    assert await group_state(session_maker, group.id) == (0, GroupStatus.FULL)
    await mod.ban_user(session_maker, people[0].id, set())  # бан участника освобождает место
    assert await group_state(session_maker, group.id) == (1, GroupStatus.OPEN)
    await check_invariants(session_maker, initial)
    await mod.ban_user(session_maker, owner.id, set())  # бан организатора закрывает группу
    assert (await group_state(session_maker, group.id))[1] == GroupStatus.CLOSED


# ---- подмена callback_data


FORGED = [
    "g:view:{g}",
    "g:members:{g}",
    "g:close:{g}",
    "g:closeok:{g}",
    "g:leave:{g}",
    "g:leaveok:{g}",
    "g:rm:{m}",
    "g:rmok:{m}",
    "m:ok:{p}",
    "m:no:{p}",
    "r:new:{g}:{o}",
    "s:grp:{g}:0",
    "a:ban:1",
    "a:svc:toggle:1",
]


async def snapshot(sm):
    async with sm() as s:
        groups = [(g.id, g.slots_free, g.status) for g in await s.scalars(select(Group))]
        members = [(m.id, m.status) for m in await s.scalars(select(Membership))]
        banned = [u.id for u in await s.scalars(select(User).where(User.is_banned.is_(True)))]
        active = [x.is_active for x in await s.scalars(select(Service))]
        reports = (await s.scalar(select(func.count()).select_from(Report))) or 0
    return groups, members, banned, active, reports


async def test_forged_callbacks_change_nothing_and_leak_nothing(dp, session_maker) -> None:
    owner, group, people = await make_arena(session_maker, users=5, total=5, free=4)
    approved_user, pending_user, stranger, other_owner, _ = people
    a = await groups_svc.request_join(session_maker, group.id, approved_user.id)
    await groups_svc.approve_member(session_maker, a.membership.id, owner.id)
    p = await groups_svc.request_join(session_maker, group.id, pending_user.id)
    before = await snapshot(session_maker)

    secrets = ["owner", str(owner.telegram_id), f"tg://user?id={owner.telegram_id}", "@u0"]
    template = dict(g=group.id, m=a.membership.id, p=p.membership.id, o=owner.id)
    bot, session = make_bot()
    # посторонний, ждущий ответа и «организатор другой группы» нажимают все чужие кнопки
    for attacker in (stranger, pending_user, other_owner):
        for pattern in FORGED:
            if pattern.startswith(("g:view", "s:grp")):
                continue  # публичные экраны проверяются отдельно ниже
            if attacker is pending_user and pattern.startswith("g:leave"):
                continue  # ждущий вправе отозвать собственную заявку
            await dp.feed_update(
                bot, callback_update(attacker.telegram_id, pattern.format(**template), "x")
            )
    assert await snapshot(session_maker) == before

    shown = "\n".join(session.texts()).lower()
    for secret in secrets:
        if secret == "@u0":
            continue
        assert secret.lower() not in shown, f"утечка {secret}"

    # публичный и «ждущий» вид карточки не раскрывает контакт и участников
    for attacker in (stranger, pending_user):
        for pattern in ("g:view:{g}", "s:grp:{g}:0"):
            await dp.feed_update(
                bot, callback_update(attacker.telegram_id, pattern.format(**template), "x")
            )
    shown = "\n".join(session.texts()).lower()
    assert "organizer:" not in shown and "@owner" not in shown and "• @" not in shown
    assert "организатор: @" not in shown
    assert await snapshot(session_maker) == before


async def test_participant_cannot_manage_group_or_other_members(dp, session_maker) -> None:
    owner, group, people = await make_arena(session_maker, users=3, total=5, free=4)
    member, other_member, _ = people
    m1 = await groups_svc.request_join(session_maker, group.id, member.id)
    m2 = await groups_svc.request_join(session_maker, group.id, other_member.id)
    await groups_svc.approve_member(session_maker, m1.membership.id, owner.id)
    await groups_svc.approve_member(session_maker, m2.membership.id, owner.id)
    before = await snapshot(session_maker)
    bot, _ = make_bot()
    for data in (
        f"g:closeok:{group.id}",
        f"g:rmok:{m2.membership.id}",  # участник пытается удалить другого
        f"g:members:{group.id}",
    ):
        await dp.feed_update(bot, callback_update(member.telegram_id, data, "u0"))
    await dp.feed_update(bot, callback_update(member.telegram_id, f"g:leaveok:{group.id}", "u0"))
    after = await snapshot(session_maker)
    assert after[1] == [
        (m1.membership.id, MembershipStatus.LEFT),
        (m2.membership.id, MembershipStatus.APPROVED),
    ]
    assert after[0] == [(group.id, before[0][0][1] + 1, GroupStatus.OPEN)]  # вышел только он сам


async def test_report_cannot_probe_membership_of_other_groups(session_maker) -> None:
    owner, group, people = await make_arena(session_maker, users=3, total=5, free=4)
    member, stranger, outsider = people
    a = await groups_svc.request_join(session_maker, group.id, member.id)
    await groups_svc.approve_member(session_maker, a.membership.id, owner.id)
    errors = set()
    for target in (member.id, outsider.id, 9999):  # уже участник, не участник, не существует
        with pytest.raises(groups_svc.GroupError) as exc:
            await mod.create_report(session_maker, stranger.id, group.id, target, "other", "x")
        errors.add(str(exc.value))
    assert len(errors) == 1  # одинаковый ответ: по нему нельзя понять, кто в группе


# ---- «залипшие» состояния ввода текста


async def test_pending_text_states_do_not_swallow_later_messages(dp, session_maker) -> None:
    owner, group, people = await make_arena(session_maker, users=2, total=5, free=4)
    member = people[0]
    a = await groups_svc.request_join(session_maker, group.id, member.id)
    await groups_svc.approve_member(session_maker, a.membership.id, owner.id)
    bot, session = make_bot()
    d = Driver(dp, bot, session)

    # 1) организатор начал удаление, не стал вводить причину и ушёл в меню
    await d.press(owner.telegram_id, f"g:rmok:{a.membership.id}", "owner")
    await d.press(owner.telegram_id, "menu:my", "owner")
    sent_before = len(session.texts_to(member.telegram_id))
    await d.say(owner.telegram_id, "просто сообщение боту", "owner")
    assert len(session.texts_to(member.telegram_id)) == sent_before  # причина не ушла

    # 2) участник начал жалобу, ушёл в меню и позже что-то написал
    other = await groups_svc.request_join(session_maker, group.id, people[1].id)
    await groups_svc.approve_member(session_maker, other.membership.id, owner.id)
    uid = people[1].telegram_id
    await d.press(uid, f"r:new:{group.id}:{owner.id}", "u1")
    await d.press(uid, "r:reason:other", "u1")
    await d.press(uid, "menu:my", "u1")
    await d.say(uid, "привет", "u1")
    async with session_maker() as s:
        assert (await s.scalar(select(func.count()).select_from(Report))) == 0


async def test_admin_text_state_is_dropped_when_admin_leaves_the_dialog(dp, session_maker) -> None:
    from tests.conftest import ADMIN_ID

    await seed_services(session_maker)
    bot, _ = make_bot()
    async with session_maker() as s:
        price_before = (await s.get(Service, 1)).price_month
    await dp.feed_update(bot, callback_update(ADMIN_ID, "a:svc:price:1", "boss"))
    await dp.feed_update(bot, callback_update(ADMIN_ID, "menu:find", "boss"))  # ушёл в поиск
    await dp.feed_update(bot, message_update(ADMIN_ID, "1", "boss"))  # случайный текст
    async with session_maker() as s:
        assert (await s.get(Service, 1)).price_month == price_before
