"""Создание группы: сервис -> свободные места -> дата списания -> инвайт-ссылка."""

import asyncio
import logging
from collections import defaultdict
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

import dates
from catalog import Service, get_service, money
from db import CREATOR_EMAIL_PLACEHOLDER, MEMBER_ACTIVE, ROLE_OWNER, STATUS_OPEN, Database
from handlers import (
    CREATE_PREFIX,
    MY_SUBS_CB,
    SERVICES_CB,
    button,
    new_group_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="create")

CANCEL_CB = "cancel_flow"
FREE_PREFIX = "free_"
BACK_PREFIX = "back_"
LATER_CB = "link_later"
LATER_WORDS = {"позже", "later", "-"}
MAX_LINK_LENGTH = 500


class CreateGroup(StatesGroup):
    free = State()
    date = State()
    link = State()


def nav_row(back: str | None) -> list:
    row = [] if back is None else button("‹ Назад", f"{BACK_PREFIX}{back}")[:1]
    return [*row, *button("✖ Отмена", CANCEL_CB)[:1]]


def with_nav(rows: list, back: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*rows, nav_row(back)])


def parse_invite_link(text: str) -> str | None:
    """None — «позже». ValueError, если это не ссылка."""
    value = text.strip()
    if value.lower() in LATER_WORDS:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc or " " in value:
        raise ValueError("Пришлите ссылку целиком (https://...) или напишите «позже».")
    if len(value) > MAX_LINK_LENGTH:
        raise ValueError("Ссылка слишком длинная.")
    return value


def creator_label(user: TgUser) -> str:
    return f"@{user.username}" if user.username else f"tg_id:{user.id}"


def free_prompt(service: Service) -> str:
    return (
        f"<b>{service.title}</b>\n"
        f"Цена за слот фиксированная: <b>{money(service.price_per_slot)}</b> в месяц "
        f"(подписка {money(service.total_price)}, мест всего {service.max_slots}).\n\n"
        "Первое место занимаете вы. Сколько мест свободно для участников?"
    )


DATE_PROMPT = (
    "Когда следующее списание за подписку? Напишите дату в формате ГГГГ-ММ-ДД, "
    "например 2026-12-05. По ней я буду напоминать об оплате."
)
LINK_PROMPT = (
    "Пришлите инвайт-ссылку в сервис для участников или напишите «позже», "
    "если пока её нет. Пароли и данные карт не присылайте."
)


def link_keyboard() -> InlineKeyboardMarkup:
    return with_nav([button("Позже", LATER_CB)], back="date")


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    message = target if isinstance(target, Message) else target.message
    if isinstance(target, CallbackQuery):
        await target.answer()
        if isinstance(message, Message):
            await message.edit_text(text, reply_markup=markup)
        return
    await message.answer(text, reply_markup=markup)


async def begin_create(target: Message | CallbackQuery, state: FSMContext, slug: str) -> None:
    service = get_service(slug)
    await state.clear()
    if service is None:
        await show(target, "Этот сервис недоступен. Выберите другой:", new_group_keyboard())
        return
    await state.set_state(CreateGroup.free)
    await state.update_data(service_slug=slug)
    free_rows = [
        button(str(n), f"{FREE_PREFIX}{n}")[0] for n in range(1, service.max_slots)
    ]  # от 1 до total - 1: одно место занимает организатор
    await show(target, free_prompt(service), with_nav([free_rows], back="services"))


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(CREATE_PREFIX)))
async def start_create_link(message: Message, command: CommandObject, state: FSMContext) -> None:
    await begin_create(message, state, (command.args or "").removeprefix(CREATE_PREFIX))


@router.callback_query(F.data.startswith(CREATE_PREFIX))
async def create_button(callback: CallbackQuery, state: FSMContext) -> None:
    await begin_create(callback, state, (callback.data or "").removeprefix(CREATE_PREFIX))


@router.callback_query(F.data == CANCEL_CB)
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Отменено. Отправьте /start, чтобы вернуться в меню.", reply_markup=None
        )


@router.callback_query(F.data.startswith(BACK_PREFIX))
async def back(callback: CallbackQuery, state: FSMContext) -> None:
    step = (callback.data or "").removeprefix(BACK_PREFIX)
    data = await state.get_data()
    if step == "services" or "service_slug" not in data:
        await state.clear()
        await show(callback, "Для какого сервиса создаём группу?", new_group_keyboard())
    elif step == "free":
        await begin_create(callback, state, data["service_slug"])
    elif step == "date":
        await state.set_state(CreateGroup.date)
        await show(callback, DATE_PROMPT, with_nav([], back="free"))
    else:
        await callback.answer()


@router.callback_query(CreateGroup.free, F.data.startswith(FREE_PREFIX))
async def got_free(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    service = get_service(data["service_slug"])
    try:
        free = int((callback.data or "").removeprefix(FREE_PREFIX))
    except ValueError:
        free = 0
    if service is None or not 1 <= free <= service.max_slots - 1:
        await callback.answer("Выберите число из списка.", show_alert=True)
        return
    await state.update_data(free=free)
    await state.set_state(CreateGroup.date)
    await show(callback, DATE_PROMPT, with_nav([], back="free"))


@router.message(CreateGroup.date, F.text)
async def got_date(message: Message, state: FSMContext) -> None:
    try:
        billing = dates.parse_billing_date(message.text or "", dates.today())
    except ValueError as error:
        await message.answer(str(error), reply_markup=with_nav([], back="free"))
        return
    await state.update_data(billing_date=billing.isoformat())
    await state.set_state(CreateGroup.link)
    await message.answer(LINK_PROMPT, reply_markup=link_keyboard())


@router.message(CreateGroup.link, F.text)
async def got_link(message: Message, state: FSMContext, db: Database) -> None:
    try:
        link = parse_invite_link(message.text or "")
    except ValueError as error:
        await message.answer(str(error), reply_markup=link_keyboard())
        return
    await save_group(message, message.from_user, state, db, link)  # type: ignore[arg-type]


@router.callback_query(CreateGroup.link, F.data == LATER_CB)
async def link_later(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await save_group(callback.message, callback.from_user, state, db, None)


@router.message(CreateGroup.date)
@router.message(CreateGroup.link)
async def not_text(message: Message) -> None:
    await message.answer("Отправьте ответ обычным текстом.")


_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def save_group(
    target: Message, user: TgUser, state: FSMContext, db: Database, link: str | None
) -> None:
    # Блокировка на пользователя: двойное нажатие «Позже» не создаст две группы
    async with _locks[user.id]:
        if await state.get_state() != CreateGroup.link.state:
            return
        data = await state.get_data()
        service = get_service(data["service_slug"])
        if service is None:
            await state.clear()
            await target.answer("Этот сервис недоступен.", reply_markup=new_group_keyboard())
            return
        free = int(data["free"])
        group = await db.create_group(
            {
                "service_slug": service.slug,
                "service_name": service.title,
                "creator_tg_username": creator_label(user),
                "creator_email": CREATOR_EMAIL_PLACEHOLDER,
                "price_per_slot": service.price_per_slot,
                "max_slots": service.max_slots,
                "total_slots": service.max_slots,
                "current_slots": service.max_slots - free,
                "occupied_slots": service.max_slots - free,
                "billing_date": data["billing_date"],
                "invite_link": link,
                "members": [
                    {
                        "tg_id": user.id,
                        "tg_username": creator_label(user),
                        "name": user.full_name,
                        "role": ROLE_OWNER,
                        "status": MEMBER_ACTIVE,
                    }
                ],
                "status": STATUS_OPEN,
            }
        )
        await state.clear()
    link_line = link if link else "пока нет, добавьте её позже"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[button("📋 Мои подписки", MY_SUBS_CB), button("‹ В меню", SERVICES_CB)]
    )
    await target.answer(
        f"✅ Группа создана и уже видна на сайте!\n\n"
        f"Сервис: {service.title}\n"
        f"Свободно мест: {free} из {service.max_slots}\n"
        f"Доля участника: {money(service.price_per_slot)} в месяц\n"
        f"Следующее списание: {data['billing_date']}\n"
        f"Инвайт-ссылка: {link_line}\n\n"
        "Когда кто-то вступит, я пришлю его email, чтобы вы отправили приглашение.",
        reply_markup=markup,
    )
    logger.info("Создана группа %s (%s)", group.get("id"), service.slug)
