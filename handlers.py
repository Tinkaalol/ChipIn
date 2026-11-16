from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import Database, Row, free_slots

router = Router(name="main")

MAX_GROUP_BUTTONS = 20  # у Telegram есть предел на размер клавиатуры

WELCOME = (
    "🦜 Привет! Я ChipIn, жёлтый попугай, который помогает скидываться на подписки.\n\n"
    "Здесь можно вместе оплачивать официальные семейные и командные подписки и экономить "
    "до 70%. Выберите сервис или загляните в открытые группы:"
)
SERVICES_CB = "services"
OPEN_GROUPS_CB = "open_groups"
SERVICE_PREFIX = "svc_"
CREATE_PREFIX = "create_"
JOIN_PREFIX = "join_"


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₸"


def button(text: str, data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=data)]


def services_keyboard(services: list[Row]) -> InlineKeyboardMarkup:
    rows = [
        button(
            f"{s['title']} · {money(s['price_per_person_kzt'])}/мес", f"{SERVICE_PREFIX}{s['slug']}"
        )
        for s in services
    ]
    rows.append(button("📂 Открытые группы", OPEN_GROUPS_CB))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_button(group: Row, title: str) -> list[InlineKeyboardButton]:
    text = f"{title} · свободно {free_slots(group)} из {group['max_slots']}"
    return button(text, f"{JOIN_PREFIX}{group['id']}")


def service_text(service: Row, groups: list[Row]) -> str:
    text = (
        f"<b>{escape(service['title'])}</b>\n"
        f"Взнос: {money(service['price_per_person_kzt'])} в месяц с человека, "
        f"мест в группе: {service['total_slots']}.\n\n"
    )
    if groups:
        return text + "Открытые группы, куда можно присоединиться:"
    return text + "Открытых групп пока нет. Создайте свою!"


def service_keyboard(service: Row, groups: list[Row]) -> InlineKeyboardMarkup:
    rows = [button("➕ Создать новую группу", f"{CREATE_PREFIX}{service['slug']}")]
    rows += [group_button(g, service["title"]) for g in groups[:MAX_GROUP_BUTTONS]]
    rows.append(button("‹ К сервисам", SERVICES_CB))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def open_groups_screen(
    groups: list[Row], titles: dict[str, str]
) -> tuple[str, InlineKeyboardMarkup]:
    rows = [group_button(g, titles.get(g["service_slug"], g["service_slug"])) for g in groups]
    rows = rows[:MAX_GROUP_BUTTONS]
    rows.append(button("‹ К сервисам", SERVICES_CB))
    text = "Открытые группы:" if groups else "Сейчас нет групп с набором. Создайте свою!"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_start(message: Message, db: Database) -> None:
    await message.answer(WELCOME, reply_markup=services_keyboard(await db.list_services()))


@router.message(CommandStart(deep_link=False))
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()  # /start всегда возвращает в начало, даже посреди заявки
    await show_start(message, db)


@router.callback_query(F.data == SERVICES_CB)
async def back_to_services(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        markup = services_keyboard(await db.list_services())
        await callback.message.edit_text(WELCOME, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith(SERVICE_PREFIX))
async def open_service(callback: CallbackQuery, db: Database) -> None:
    slug = (callback.data or "").removeprefix(SERVICE_PREFIX)
    service = await db.get_service(slug)
    if service is None:
        await callback.answer("Этот сервис больше недоступен.", show_alert=True)
        return
    groups = await db.list_open_groups(slug)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            service_text(service, groups), reply_markup=service_keyboard(service, groups)
        )
    await callback.answer()


@router.callback_query(F.data == OPEN_GROUPS_CB)
async def open_groups(callback: CallbackQuery, db: Database) -> None:
    groups = await db.list_open_groups()
    titles = {s["slug"]: s["title"] for s in await db.list_services()}
    text, markup = open_groups_screen(groups, titles)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()
