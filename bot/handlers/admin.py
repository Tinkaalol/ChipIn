from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, ui
from bot.config import get_settings
from bot.db.session import session_maker
from bot.services import catalog
from bot.services import groups as groups_svc
from bot.services import moderation as mod
from bot.texts import contact_html, money

router = Router(name="admin")

BAN = "a:ban:"
DISMISS = "a:dismiss:"
SERVICES_LIST = "a:svc:list"
SERVICE_TOGGLE = "a:svc:toggle:"
SERVICE_PRICE = "a:svc:price:"
SERVICE_ADD = "a:svc:add"
CANCEL_ROW = [("« Назад", SERVICES_LIST), ("Отмена", SERVICES_LIST)]


def admin_ids() -> set[int]:
    return set(get_settings().admin_ids)


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in admin_ids()


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminFlow(StatesGroup):
    add_service = State()
    edit_price = State()


def report_text(view: mod.ReportView) -> str:
    r = view.report
    banned = " (уже забанен)" if view.target.is_banned else ""
    group = f"{view.service.name} {view.service.plan_name}" if view.service else "—"
    return (
        f"Жалоба #{r.id}\n"
        f"На: {contact_html(view.target.telegram_id, view.target.username)}{banned}\n"
        f"От: {contact_html(view.reporter.telegram_id, view.reporter.username)}\n"
        f"Группа: {escape(group)}\n"
        f"Причина: {escape(r.reason)}"
    )


async def announce_ban(bot: Bot, result: mod.BanResult) -> None:
    await ui.notify(bot, result.user.telegram_id, "Ваш доступ к боту ограничен администратором.")
    for closed in result.closed:
        for chat_id in closed.notify_ids:
            await ui.notify(
                bot, chat_id, f"Группа {closed.title} закрыта: организатор заблокирован."
            )
    for left in result.left:
        await ui.notify(
            bot,
            left.owner_telegram_id,
            f"Участник группы {left.title} заблокирован и удалён из неё. Место свободно.",
        )


@router.message(Command("reports"))
async def cmd_reports(message: Message) -> None:
    views = await mod.list_open_reports(session_maker)
    if not views:
        await message.answer("Открытых жалоб нет.")
        return
    for view in views:
        markup = keyboards.build(
            [[("Забанить", f"{BAN}{view.report.id}"), ("Отклонить", f"{DISMISS}{view.report.id}")]]
        )
        await message.answer(report_text(view), reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith((BAN, DISMISS)))
async def resolve(callback: CallbackQuery, bot: Bot) -> None:
    data = callback.data or ""
    ban = data.startswith(BAN)
    report_id = int(data.removeprefix(BAN if ban else DISMISS))
    try:
        outcome = await mod.resolve_report(session_maker, report_id, ban, admin_ids())
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if outcome.ban is not None:
        await announce_ban(bot, outcome.ban)
    verdict = "✅ Пользователь забанен" if ban else "Жалоба отклонена"
    original = callback.message.html_text if callback.message else ""  # type: ignore[union-attr]
    await ui.show(callback, f"{original}\n\n{verdict}", None, html=True)


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot) -> None:
    try:
        user = await mod.find_user_by_username(session_maker, command.args or "")
        result = await mod.ban_user(session_maker, user.id, admin_ids())
    except groups_svc.GroupError as e:
        await message.answer(str(e))
        return
    await message.answer(f"@{user.username} забанен. Закрыто групп: {len(result.closed)}.")
    await announce_ban(bot, result)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot) -> None:
    try:
        user = await mod.find_user_by_username(session_maker, command.args or "")
        await mod.unban_user(session_maker, user.id)
    except groups_svc.GroupError as e:
        await message.answer(str(e))
        return
    await message.answer(f"@{user.username} разбанен. Закрытые группы не восстанавливаются.")
    await ui.notify(bot, user.telegram_id, "Ваш доступ к боту восстановлен.")


async def services_screen() -> tuple[str, InlineKeyboardMarkup]:
    services = await catalog.list_all_services(session_maker)
    lines = ["Каталог сервисов:"]
    rows = []
    for s in services:
        state = "активен" if s.is_active else "выключен"
        lines.append(
            f"{s.id}. {s.name} {s.plan_name} — {money(s.price_month, s.currency)}, "
            f"до {s.max_members} мест, {state}"
        )
        toggle = "Выключить" if s.is_active else "Включить"
        rows.append(
            [(f"{toggle} {s.name}", f"{SERVICE_TOGGLE}{s.id}"), ("Цена", f"{SERVICE_PRICE}{s.id}")]
        )
    rows.append([("Добавить сервис", SERVICE_ADD)])
    rows.append([("В главное меню", keyboards.HOME)])
    return "\n".join(lines), keyboards.build(rows)


@router.message(Command("services"))
async def cmd_services(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await services_screen()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == SERVICES_LIST)
async def back_to_services(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await services_screen()
    await ui.show(callback, text, markup)


@router.callback_query(F.data.startswith(SERVICE_TOGGLE))
async def toggle_service(callback: CallbackQuery) -> None:
    service_id = int((callback.data or "").removeprefix(SERVICE_TOGGLE))
    services = {s.id: s for s in await catalog.list_all_services(session_maker)}
    current = services.get(service_id)
    if current is None:
        await callback.answer("Сервис не найден.", show_alert=True)
        return
    await catalog.set_service_active(session_maker, service_id, not current.is_active)
    text, markup = await services_screen()
    await ui.show(callback, text, markup)


@router.callback_query(F.data.startswith(SERVICE_PRICE))
async def ask_price(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = int((callback.data or "").removeprefix(SERVICE_PRICE))
    await state.set_state(AdminFlow.edit_price)
    await state.update_data(service_id=service_id)
    text = "Введите новую цену в месяц, например 349. Она применится к новым группам."
    await ui.show(callback, text, keyboards.build([CANCEL_ROW]))


@router.callback_query(F.data == SERVICE_ADD)
async def ask_new_service(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFlow.add_service)
    text = "Отправьте одной строкой:\nНазвание | Тариф | Цена в месяц | Макс. участников"
    await ui.show(callback, text, keyboards.build([CANCEL_ROW]))


@router.message(StateFilter(AdminFlow.edit_price), F.text, ~F.text.startswith("/"))
async def set_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        price = catalog.parse_price(message.text or "")
        await catalog.set_service_price(session_maker, data["service_id"], price)
    except groups_svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.build([CANCEL_ROW]))
        return
    await state.clear()
    text, markup = await services_screen()
    await message.answer(text, reply_markup=markup)


@router.message(StateFilter(AdminFlow.add_service), F.text, ~F.text.startswith("/"))
async def create_service(message: Message, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|")]
    try:
        if len(parts) != 4:
            raise groups_svc.GroupError("Нужно четыре поля через |, как в примере выше.")
        price = catalog.parse_price(parts[2])
        try:
            max_members = int(parts[3])
        except ValueError:
            raise groups_svc.GroupError("Максимум участников — целое число.") from None
        await catalog.add_service(session_maker, parts[0], parts[1], price, max_members)
    except groups_svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.build([CANCEL_ROW]))
        return
    await state.clear()
    text, markup = await services_screen()
    await message.answer(text, reply_markup=markup)
