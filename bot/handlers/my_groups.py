from datetime import date, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import keyboards, ui
from bot.services.reminders import ALMATY, next_payment_date
from bot.texts import contact_html, money
from shared.db.models import GroupStatus, MembershipStatus, User
from shared.db.session import session_maker
from shared.services import groups as groups_svc
from shared.services import memberships as svc
from shared.services.groups import Role
from shared.services.users import chat_id_for, register_user

router = Router(name="my_groups")

VIEW = "g:view:"
MEMBERS = "g:members:"
CLOSE = "g:close:"
CLOSE_OK = "g:closeok:"
LEAVE = "g:leave:"
LEAVE_OK = "g:leaveok:"
REMOVE = "g:rm:"  # g:rm:<membership_id>
REMOVE_OK = "g:rmok:"
REMOVE_SKIP = "g:rmskip"
REPORT = "r:new:"  # r:new:<group_id>:<user_id>
APPLICATION_DECIDE_PREFIX = "m:"  # m:ok:<membership_id> / m:no:<membership_id>

STATUS_LABELS = {
    GroupStatus.OPEN: "идёт набор",
    GroupStatus.FULL: "мест нет",
    GroupStatus.CLOSED: "закрыта",
}
HOME_ROW = [("« Назад", keyboards.MY_GROUPS), ("Отмена", keyboards.HOME)]


async def _current_user_id(callback: CallbackQuery) -> int:
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    return user.id


def _gid(callback: CallbackQuery, prefix: str) -> int:
    return int((callback.data or "").removeprefix(prefix))


def _label(user: User) -> str:
    return f"@{user.username}" if user.username else f"пользователя {user.telegram_id}"


def _back_to_group(group_id: int) -> list[tuple[str, str]]:
    return [("« Назад", f"{VIEW}{group_id}"), ("Отмена", keyboards.HOME)]


@router.callback_query(F.data == keyboards.MY_GROUPS)
async def show_my_groups(callback: CallbackQuery) -> None:
    user_id = await _current_user_id(callback)
    entries = await svc.list_user_groups(session_maker, user_id)
    rows = []
    for e in entries:
        if e.role == "owner":
            role = "организатор"
        elif e.membership_status == MembershipStatus.PENDING:
            role = "заявка отправлена"
        else:
            role = "участник"
        rows.append([(f"{e.service.name} {e.service.plan_name} · {role}", f"{VIEW}{e.group.id}")])
    rows.append([("« Назад", keyboards.HOME)])
    text = (
        "Ваши группы:"
        if entries
        else "У вас пока нет групп. Создайте свою или найдите подходящую в поиске."
    )
    await ui.show(callback, text, keyboards.build(rows))


@router.callback_query(F.data.startswith(VIEW))
async def view_group(callback: CallbackQuery) -> None:
    await render_card(callback, _gid(callback, VIEW))


def _participant(user: User) -> str:
    # В карточке только username; без него контакт не раскрываем
    return f"@{escape(user.username)}" if user.username else "участник без username"


def build_card(
    card: svc.GroupCard, list_page: int | None, today: date
) -> tuple[str, list[list[tuple[str, str]]]]:
    """Текст и кнопки карточки группы; вид зависит от роли смотрящего."""
    g, s = card.group, card.service
    next_date = next_payment_date(g.billing_day, today)
    lines = [
        f"{escape(s.name)} {escape(s.plan_name)} — {STATUS_LABELS[g.status]}",
        f"С человека: {money(g.price_per_member, s.currency)} в месяц "
        f"(подписка целиком: {money(s.price_month, s.currency)})",
        f"Участников: {g.slots_total - g.slots_free} из {g.slots_total} "
        f"(с организатором), свободно мест: {g.slots_free}",
        f"Оплата: {g.billing_day}-го числа, ближайшая — {next_date:%d.%m.%Y}",
    ]
    rows: list[list[tuple[str, str]]] = []
    if card.role == Role.OWNER:
        lines.append("\nВы — организатор этой группы.")
        lines.append(_people("Участники", card.participants, "Участников пока нет."))
        if card.applicants:
            lines.append(_people("Заявки ждут ответа", [a.user for a in card.applicants]))
        for a in card.applicants:
            who = f"@{a.user.username}" if a.user.username else "без username"
            rows.append(
                [
                    (f"✅ Принять {who}", f"{APPLICATION_DECIDE_PREFIX}ok:{a.membership_id}"),
                    ("❌ Отклонить", f"{APPLICATION_DECIDE_PREFIX}no:{a.membership_id}"),
                ]
            )
        rows.append([("Участники", f"{MEMBERS}{g.id}"), ("Закрыть группу", f"{CLOSE}{g.id}")])
    elif card.role == Role.MEMBER and card.owner is not None:
        contact = contact_html(card.owner.telegram_id, card.owner.username)
        lines.append(f"\nОрганизатор: {contact}")
        lines.append("Деньги переводите организатору напрямую, бот их не принимает.")
        lines.append(_people("Участники", card.participants))
        rows.append([("Выйти из группы", f"{LEAVE}{g.id}")])
        rows.append([("Пожаловаться на организатора", f"{REPORT}{g.id}:{card.owner.id}")])
    elif card.role == Role.PENDING:
        lines.append("\nЗаявка отправлена, ждём ответа.")
        rows.append([("Отозвать заявку", f"{LEAVE}{g.id}")])
    else:
        lines.append("\nКонтакт организатора откроется после того, как он примет вашу заявку.")
        rows.append([("Подать заявку", f"{keyboards.SEARCH_APPLY}{g.id}")])
    if list_page is None:
        rows.append(HOME_ROW)
    else:
        back = f"{keyboards.SEARCH_SERVICE}{s.id}:{list_page}"
        rows.append([("« Назад к списку", back), ("Отмена", keyboards.HOME)])
    return "\n".join(lines), rows


def _people(title: str, users: list[User], empty: str = "") -> str:
    if not users:
        return empty
    return f"{title}:\n" + "\n".join(f"• {_participant(u)}" for u in users)


async def render_card(callback: CallbackQuery, group_id: int, list_page: int | None = None) -> None:
    """Показывает карточку группы. Состав данных определяет сервер по роли, а не callback."""
    card = await svc.get_group_card(session_maker, group_id, await _current_user_id(callback))
    if card is None:
        await callback.answer("Эта группа недоступна.", show_alert=True)
        return
    text, rows = build_card(card, list_page, datetime.now(ALMATY).date())
    await ui.show(callback, text, keyboards.build(rows), html=True)


class RemoveFlow(StatesGroup):
    reason = State()


async def render_members(callback: CallbackQuery, group_id: int) -> None:
    try:
        members = await svc.list_members(session_maker, group_id, await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if members:
        listing = "\n".join(
            f"• {contact_html(m.user.telegram_id, m.user.username)}" for m in members
        )
        text = f"Участники группы:\n{listing}"
        rows = [
            [
                (f"Удалить {_short(m.user)}", f"{REMOVE}{m.membership_id}"),
                ("Пожаловаться", f"{REPORT}{group_id}:{m.user.id}"),
            ]
            for m in members
        ]
    else:
        text = "Пока нет принятых участников."
        rows = []
    rows.append(_back_to_group(group_id))
    await ui.show(callback, text, keyboards.build(rows), html=True)


@router.callback_query(F.data.startswith(MEMBERS))
async def show_members(callback: CallbackQuery) -> None:
    await render_members(callback, _gid(callback, MEMBERS))


def _short(user: User) -> str:
    return f"@{user.username}" if user.username else "участника"


@router.callback_query(F.data.startswith(REMOVE_OK))
async def remove_confirmed(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    try:
        removal = await svc.remove_member(
            session_maker, _gid(callback, REMOVE_OK), await _current_user_id(callback)
        )
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.set_state(RemoveFlow.reason)
    await state.update_data(membership_id=removal.membership.id, group_id=removal.group.id)
    text = (
        f"{_short(removal.member).capitalize()} удалён из группы, ему отправлено уведомление.\n"
        "Если хотите, напишите причину — я передам её. Это необязательно."
    )
    await ui.show(callback, text, keyboards.build([[("Без причины", REMOVE_SKIP)]]))


@router.callback_query(F.data == REMOVE_SKIP, RemoveFlow.reason)
async def remove_without_reason(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = (await state.get_data())["group_id"]
    await state.clear()
    await render_members(callback, group_id)


@router.message(RemoveFlow.reason, F.text, ~F.text.startswith("/"))
async def removal_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    user = await register_user(session_maker, message.from_user.id, message.from_user.username)
    try:
        removal = await svc.set_removal_reason(
            session_maker, data["membership_id"], user.id, message.text or ""
        )
    except groups_svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.build([[("Без причины", REMOVE_SKIP)]]))
        return
    await state.clear()
    await message.answer(
        "Причина передана.",
        reply_markup=keyboards.build([[("« К участникам", f"{MEMBERS}{removal.group.id}")]]),
    )


@router.callback_query(F.data.startswith(REMOVE))
async def remove_ask(callback: CallbackQuery) -> None:
    membership_id = _gid(callback, REMOVE)
    try:
        removal = await svc.get_removable_member(
            session_maker, membership_id, await _current_user_id(callback)
        )
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    who = f"@{removal.member.username}" if removal.member.username else "этого участника"
    rows = [
        [("Да, удалить", f"{REMOVE_OK}{membership_id}")],
        [("Отмена", f"{MEMBERS}{removal.group.id}")],
    ]
    await ui.show(
        callback, f"Удалить {who} из группы? Он получит уведомление.", keyboards.build(rows)
    )


@router.callback_query(F.data.startswith(CLOSE_OK))
async def close_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    group_id = _gid(callback, CLOSE_OK)
    try:
        closure = await svc.close_group(session_maker, group_id, await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    title = f"{closure.service.name} {closure.service.plan_name}"
    await ui.show(
        callback,
        f"Группа {title} закрыта.",
        keyboards.build([[("« К моим группам", keyboards.MY_GROUPS)]]),
    )


@router.callback_query(F.data.startswith(CLOSE))
async def close_ask(callback: CallbackQuery) -> None:
    group_id = _gid(callback, CLOSE)
    role = await groups_svc.get_user_role(session_maker, group_id, await _current_user_id(callback))
    if role != Role.OWNER:
        await callback.answer("Это не ваша группа.", show_alert=True)
        return
    rows = [[("Да, закрыть", f"{CLOSE_OK}{group_id}")], _back_to_group(group_id)]
    text = "Закрыть группу? Новые заявки приниматься не будут, участники получат уведомление."
    await ui.show(callback, text, keyboards.build(rows))


@router.callback_query(F.data.startswith(LEAVE_OK))
async def leave_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    group_id = _gid(callback, LEAVE_OK)
    try:
        dep = await svc.leave_group(session_maker, group_id, await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    title = f"{dep.service.name} {dep.service.plan_name}"
    await ui.show(
        callback,
        f"Вы вышли из группы {title}." if dep.was_approved else "Заявка отозвана.",
        keyboards.build([[("« К моим группам", keyboards.MY_GROUPS)]]),
    )


@router.callback_query(F.data.startswith(LEAVE))
async def leave_ask(callback: CallbackQuery) -> None:
    group_id = _gid(callback, LEAVE)
    role = await groups_svc.get_user_role(session_maker, group_id, await _current_user_id(callback))
    if role not in (Role.MEMBER, Role.PENDING):
        await callback.answer("Вы не состоите в этой группе.", show_alert=True)
        return
    rows = [[("Да, выйти", f"{LEAVE_OK}{group_id}")], _back_to_group(group_id)]
    await ui.show(callback, "Точно выйти из группы?", keyboards.build(rows))


@router.callback_query(F.data.startswith(APPLICATION_DECIDE_PREFIX))
async def decide_application(callback: CallbackQuery, bot: Bot) -> None:
    _, action, membership_id = (callback.data or "").split(":")
    approve = action == "ok"
    try:
        decide = groups_svc.approve_member if approve else groups_svc.reject_member
        d = await decide(session_maker, int(membership_id), await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if approve:
        verdict = "✅ Принята"
        if chat_id_for(d.member) is None:
            verdict += "\nНе удалось уведомить участника, свяжитесь с ним сами."
        if d.group.status == GroupStatus.FULL:
            verdict += "\nВсе места заняты, набор закрыт."
    else:
        verdict = "❌ Отклонена"
    original = callback.message.html_text if callback.message else ""  # type: ignore[union-attr]
    back = keyboards.build([[("« К моим группам", keyboards.MY_GROUPS)]])
    await ui.show(callback, f"{original}\n\n{verdict}", back, html=True)
