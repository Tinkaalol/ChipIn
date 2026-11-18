import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import dates
from catalog import CATALOG, Service, get_service, money, title_of
from db import ROLE_OWNER, Database, Row, free_slots, is_owner, members_of

logger = logging.getLogger(__name__)
router = Router(name="main")

ERROR_TEXT = "Что-то пошло не так. Попробуйте ещё раз через минуту."
MAX_BUTTONS = 20  # у Telegram есть предел на размер клавиатуры
MAX_SUBS_SHOWN = 15

WELCOME = (
    "🦜 Привет! Я ChipIn, жёлтый попугай, который помогает скидываться на подписки.\n\n"
    "Здесь можно вместе оплачивать официальные семейные и командные подписки и экономить "
    "до 70%. Цены фиксированные. Выберите сервис или действие:"
)
SERVICES_CB = "services"
OPEN_GROUPS_CB = "open_groups"
NEW_GROUP_CB = "newgroup"
MY_SUBS_CB = "my_subs"
SERVICE_PREFIX = "svc_"
CREATE_PREFIX = "create_"
JOIN_PREFIX = "join_"


def button(text: str, data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=data)]


def service_label(service: Service) -> str:
    return f"{service.title} · {money(service.price_per_slot)}/слот"


def menu_keyboard() -> InlineKeyboardMarkup:
    rows = [button(service_label(s), f"{SERVICE_PREFIX}{s.slug}") for s in CATALOG]
    rows.append(button("📂 Открытые группы", OPEN_GROUPS_CB))
    rows.append(button("➕ Создать группу", NEW_GROUP_CB))
    rows.append(button("📋 Мои подписки", MY_SUBS_CB))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu() -> list[InlineKeyboardButton]:
    return button("‹ В меню", SERVICES_CB)


def group_button(group: Row) -> list[InlineKeyboardButton]:
    title = title_of(group["service_slug"])
    text = f"{title} · свободно {free_slots(group)} из {group['max_slots']}"
    return button(text, f"{JOIN_PREFIX}{group['id']}")


def service_text(service: Service, groups: list[Row]) -> str:
    text = (
        f"<b>{escape(service.title)}</b>\n"
        f"Подписка стоит {money(service.total_price)} в месяц, "
        f"мест в группе: {service.max_slots}.\n"
        f"Ваша доля: <b>{money(service.price_per_slot)}</b> в месяц.\n\n"
    )
    if groups:
        return text + "Открытые группы, куда можно присоединиться:"
    return text + "Открытых групп пока нет. Создайте свою!"


def service_keyboard(service: Service, groups: list[Row]) -> InlineKeyboardMarkup:
    rows = [button("➕ Создать новую группу", f"{CREATE_PREFIX}{service.slug}")]
    rows += [group_button(g) for g in groups[:MAX_BUTTONS]]
    rows.append(back_to_menu())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def new_group_keyboard() -> InlineKeyboardMarkup:
    rows = [button(service_label(s), f"{CREATE_PREFIX}{s.slug}") for s in CATALOG]
    rows.append(back_to_menu())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def member_name(member: Row) -> str:
    return member.get("tg_username") or member.get("name") or f"ID {member.get('tg_id')}"


def subscription_text(group: Row, tg_id: int, username: str | None, now: date) -> str:
    """Карточка одной группы для /my_subs: роль, доля и следующее списание."""
    service = get_service(group["service_slug"])
    price = group.get("price_per_slot") or (service.price_per_slot if service else None)
    owner = is_owner(group, tg_id, username)
    role = "вы организатор" if owner else "вы участник"
    lines = [f"<b>{escape(title_of(group['service_slug']))}</b> · {role}"]
    if price:
        lines.append(f"Ваша доля: {money(int(price))} в месяц")
    if group.get("billing_date"):
        charge = dates.next_charge_date(date.fromisoformat(group["billing_date"]), now)
        lines.append(f"Следующее списание: {charge:%d.%m.%Y}")
    lines.append(f"Мест: занято {group['current_slots']} из {group['max_slots']}")
    if owner:
        others = [member_name(m) for m in members_of(group) if m.get("role") != ROLE_OWNER]
        lines.append("Участники: " + (escape(", ".join(others)) if others else "пока никого"))
        if group.get("invite_link"):
            lines.append(f"Инвайт-ссылка: {escape(group['invite_link'])}")
    else:
        lines.append(f"Организатор: {escape(group.get('creator_tg_username') or '—')}")
    return "\n".join(lines)


def my_subs_text(groups: list[Row], tg_id: int, username: str | None, now: date) -> str:
    if not groups:
        return "У вас пока нет подписок. Выберите сервис в меню: /start"
    groups = groups[:MAX_SUBS_SHOWN]
    return "<b>Мои подписки</b>\n\n" + "\n\n".join(
        subscription_text(g, tg_id, username, now) for g in groups
    )


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Любая ошибка (например, Supabase недоступен) не оставляет пользователя без ответа."""
    logger.error("Ошибка в обработчике", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(ERROR_TEXT, show_alert=True)
        elif update.message is not None:
            await update.message.answer(ERROR_TEXT)
    except TelegramAPIError:
        logger.warning("Не удалось сообщить об ошибке пользователю", exc_info=True)
    return True


@router.message(CommandStart(deep_link=False))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()  # /start всегда возвращает в начало, даже посреди заявки
    await message.answer(WELCOME, reply_markup=menu_keyboard())


@router.callback_query(F.data == SERVICES_CB)
async def back_to_services(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(WELCOME, reply_markup=menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith(SERVICE_PREFIX))
async def open_service(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    service = get_service((callback.data or "").removeprefix(SERVICE_PREFIX))
    if service is None:
        await callback.answer("Этот сервис больше недоступен.", show_alert=True)
        return
    groups = await db.list_open_groups(service.slug)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            service_text(service, groups), reply_markup=service_keyboard(service, groups)
        )
    await callback.answer()


@router.callback_query(F.data == OPEN_GROUPS_CB)
async def open_groups(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    groups = await db.list_open_groups()
    rows = [group_button(g) for g in groups[:MAX_BUTTONS]] + [back_to_menu()]
    text = "Открытые группы:" if groups else "Сейчас нет групп с набором. Создайте свою!"
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    await callback.answer()


@router.callback_query(F.data == NEW_GROUP_CB)
async def choose_service_to_create(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Для какого сервиса создаём группу? Цены фиксированные.",
            reply_markup=new_group_keyboard(),
        )
    await callback.answer()


async def show_my_subs(target: Message, user_id: int, username: str | None, db: Database) -> None:
    groups = await db.list_user_groups(user_id, username)
    text = my_subs_text(groups, user_id, username, dates.today())
    markup = InlineKeyboardMarkup(inline_keyboard=[back_to_menu()])
    await target.answer(text, reply_markup=markup)


@router.message(Command("my_subs"))
async def cmd_my_subs(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is not None:
        await show_my_subs(message, message.from_user.id, message.from_user.username, db)


@router.callback_query(F.data == MY_SUBS_CB)
async def my_subs_button(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        user = callback.from_user
        await show_my_subs(callback.message, user.id, user.username, db)
