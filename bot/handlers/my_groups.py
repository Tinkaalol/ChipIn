from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot import keyboards, ui
from bot.db.models import GroupStatus, MembershipStatus, User
from bot.db.session import session_maker
from bot.services import groups as groups_svc
from bot.services import memberships as svc
from bot.services.users import register_user
from bot.texts import contact_html, money

router = Router(name="my_groups")

VIEW = "g:view:"
MEMBERS = "g:members:"
CLOSE = "g:close:"
CLOSE_OK = "g:closeok:"
LEAVE = "g:leave:"
LEAVE_OK = "g:leaveok:"
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
    await render_group(callback, _gid(callback, VIEW))


async def render_group(callback: CallbackQuery, group_id: int) -> None:
    view = await svc.get_group_view(session_maker, group_id, await _current_user_id(callback))
    if view is None:
        await callback.answer("Эта группа недоступна.", show_alert=True)
        return
    g, s = view.group, view.service
    lines = [
        f"{s.name} {s.plan_name} — {STATUS_LABELS[g.status]}",
        f"Свободно мест: {g.slots_free} из {g.slots_total}",
        f"С участника: {money(g.price_per_member, s.currency)} в месяц",
        f"Оплата: {g.billing_day}-го числа",
    ]
    if view.role == "owner":
        rows = [
            [("Участники", f"{MEMBERS}{g.id}")],
            [("Закрыть группу", f"{CLOSE}{g.id}")],
            HOME_ROW,
        ]
    else:
        if view.owner_contact is not None:
            contact = contact_html(view.owner_contact.telegram_id, view.owner_contact.username)
            lines.append(f"Организатор: {contact}")
            lines.append("Деньги переводите организатору напрямую, бот их не принимает.")
            leave_text = "Выйти из группы"
        else:
            lines.append("Заявка на рассмотрении у организатора.")
            leave_text = "Отозвать заявку"
        rows = [[(leave_text, f"{LEAVE}{g.id}")]]
        if view.owner_contact is not None:
            rows.append(
                [("Пожаловаться на организатора", f"{REPORT}{g.id}:{view.owner_contact.id}")]
            )
        rows.append(HOME_ROW)
    await ui.show(callback, "\n".join(lines), keyboards.build(rows), html=True)


@router.callback_query(F.data.startswith(MEMBERS))
async def show_members(callback: CallbackQuery) -> None:
    group_id = _gid(callback, MEMBERS)
    try:
        members = await svc.list_members(session_maker, group_id, await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if members:
        listing = "\n".join(f"• {contact_html(m.telegram_id, m.username)}" for m in members)
        text = f"Участники группы:\n{listing}"
        rows = [[(f"Пожаловаться на {_label(m)}", f"{REPORT}{group_id}:{m.id}")] for m in members]
    else:
        text = "Пока нет принятых участников."
        rows = []
    rows.append(_back_to_group(group_id))
    await ui.show(callback, text, keyboards.build(rows), html=True)


@router.callback_query(F.data.startswith(CLOSE_OK))
async def close_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    group_id = _gid(callback, CLOSE_OK)
    try:
        closure = await svc.close_group(session_maker, group_id, await _current_user_id(callback))
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    title = f"{closure.service.name} {closure.service.plan_name}"
    for chat_id in closure.notify_ids:
        await ui.notify(bot, chat_id, f"Организатор закрыл группу {title}.")
    await ui.show(
        callback,
        f"Группа {title} закрыта.",
        keyboards.build([[("« К моим группам", keyboards.MY_GROUPS)]]),
    )


@router.callback_query(F.data.startswith(CLOSE))
async def close_ask(callback: CallbackQuery) -> None:
    group_id = _gid(callback, CLOSE)
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
    who = contact_html(dep.user.telegram_id, dep.user.username)
    if dep.was_approved:
        owner_text = f"Участник {who} вышел из группы {title}. Место снова свободно."
    else:
        owner_text = f"{who} отозвал заявку в группу {title}."
    await ui.notify(bot, dep.owner.telegram_id, owner_text, html=True)
    await ui.show(
        callback,
        f"Вы вышли из группы {title}." if dep.was_approved else "Заявка отозвана.",
        keyboards.build([[("« К моим группам", keyboards.MY_GROUPS)]]),
    )


@router.callback_query(F.data.startswith(LEAVE))
async def leave_ask(callback: CallbackQuery) -> None:
    group_id = _gid(callback, LEAVE)
    rows = [[("Да, выйти", f"{LEAVE_OK}{group_id}")], _back_to_group(group_id)]
    await ui.show(callback, "Точно выйти из группы?", keyboards.build(rows))


@router.callback_query(F.data.startswith(APPLICATION_DECIDE_PREFIX))
async def decide_application(callback: CallbackQuery, bot: Bot) -> None:
    _, action, membership_id = (callback.data or "").split(":")
    approve = action == "ok"
    try:
        d = await svc.decide_application(
            session_maker, int(membership_id), await _current_user_id(callback), approve
        )
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    title = f"{d.service.name} {d.service.plan_name}"
    if approve:
        owner_contact = contact_html(d.owner.telegram_id, d.owner.username)
        await ui.notify(
            bot,
            d.member.telegram_id,
            f"Ваша заявка в группу {escape(title)} принята.\n"
            f"Организатор: {owner_contact}\n"
            f"Оплата {d.group.billing_day}-го числа каждого месяца: "
            f"{money(d.group.price_per_member, d.service.currency)}.\n"
            "Деньги переводите организатору напрямую, бот их не принимает.",
            html=True,
        )
        verdict = "✅ Принята"
        if d.group.status == GroupStatus.FULL:
            verdict += "\nВсе места заняты, набор закрыт."
    else:
        await ui.notify(
            bot, d.member.telegram_id, f"Организатор отклонил вашу заявку в группу {title}."
        )
        verdict = "❌ Отклонена"
    original = callback.message.html_text if callback.message else ""  # type: ignore[union-attr]
    await ui.show(callback, f"{original}\n\n{verdict}", None, html=True)
