"""Вступление в группу: ссылка join_<id> с сайта или выбор в боте -> email -> место занято."""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

import dates
from catalog import get_service, money, title_of
from db import (
    MEMBER_ACTIVE,
    ROLE_MEMBER,
    ROLE_OWNER,
    AlreadyMember,
    Database,
    GroupUnavailable,
    Row,
    free_slots,
    has_member,
    is_joinable,
    is_owner,
    members_of,
)
from handlers import CREATE_PREFIX, JOIN_PREFIX, MY_SUBS_CB, SERVICES_CB, button

logger = logging.getLogger(__name__)
router = Router(name="join")

CANCEL_JOIN_CB = "cancel_join"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 254


class JoinGroup(StatesGroup):
    email = State()


def valid_email(text: str) -> bool:
    value = text.strip()
    return len(value) <= MAX_EMAIL_LENGTH and EMAIL_RE.fullmatch(value) is not None


def member_entry(user: TgUser, email: str) -> Row:
    return {
        "tg_id": user.id,
        "tg_username": f"@{user.username}" if user.username else None,
        "name": user.full_name,
        "email": email,
        "role": ROLE_MEMBER,
        "status": MEMBER_ACTIVE,
        "joined_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[button("✖ Отмена", CANCEL_JOIN_CB)])


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[button("📋 Мои подписки", MY_SUBS_CB), button("‹ В меню", SERVICES_CB)]
    )


def email_prompt(group: Row) -> str:
    service = get_service(group["service_slug"])
    share = f"Ваша доля: {money(service.price_per_slot)} в месяц.\n" if service else ""
    return (
        f"Вы вступаете в группу <b>{escape(title_of(group['service_slug']))}</b>.\n{share}\n"
        "Введите ваш личный email, на него придёт официальное приглашение от сервиса.\n"
        "🔒 Чужие пароли мы не используем и не просим: доступ выдаётся на ваш личный аккаунт. "
        "Пароли и данные карт в чат не отправляйте."
    )


def joined_text(group: Row, email: str, now: date) -> str:
    """Что получает участник: доля, дата списания и приглашение."""
    service = get_service(group["service_slug"])
    price = group.get("price_per_slot") or (service.price_per_slot if service else None)
    lines = [f"✅ Вы в группе {escape(title_of(group['service_slug']))}!"]
    if price:
        lines.append(f"Ваша доля: {money(int(price))} в месяц.")
    if group.get("billing_date"):
        charge = dates.next_charge_date(date.fromisoformat(group["billing_date"]), now)
        lines.append(f"Следующее списание: {charge:%d.%m.%Y}.")
    if group.get("invite_link"):
        lines.append(f"Ссылка-приглашение: {escape(group['invite_link'])}")
    else:
        lines.append(f"Приглашение придёт на {escape(email)}.")
    lines.append(f"Организатор: {escape(group.get('creator_tg_username') or '—')}")
    return "\n".join(lines)


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    message = target if isinstance(target, Message) else target.message
    if isinstance(target, CallbackQuery):
        await target.answer()
    if isinstance(message, Message):
        await message.answer(text, reply_markup=markup)


def create_offer(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            button("➕ Создать новую группу", f"{CREATE_PREFIX}{slug}"),
            *[button("‹ В меню", SERVICES_CB)],
        ]
    )


async def begin_join(
    target: Message | CallbackQuery, state: FSMContext, db: Database, group_id: str, user: TgUser
) -> None:
    await state.clear()
    group = await db.get_group(group_id)
    if group is None:
        await show(target, "Такой группы не нашлось. Посмотрите другие:", menu_keyboard())
        return
    if is_owner(group, user.id, user.username):
        await show(target, "Это ваша группа, вступать в неё не нужно.", menu_keyboard())
        return
    if has_member(group, user.id):
        await show(target, "Вы уже участник этой группы.", menu_keyboard())
        return
    if not is_joinable(group):
        text = "😔 К сожалению, в этой группе уже нет мест. Хотите создать новую для этого сервиса?"
        await show(target, text, create_offer(group["service_slug"]))
        return
    await state.set_state(JoinGroup.email)
    await state.update_data(group_id=group_id)
    await show(target, email_prompt(group), cancel_keyboard())


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(JOIN_PREFIX)))
async def start_join_link(
    message: Message, command: CommandObject, state: FSMContext, db: Database
) -> None:
    group_id = (command.args or "").removeprefix(JOIN_PREFIX)
    await begin_join(message, state, db, group_id, message.from_user)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith(JOIN_PREFIX))
async def join_button(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    group_id = (callback.data or "").removeprefix(JOIN_PREFIX)
    await begin_join(callback, state, db, group_id, callback.from_user)


@router.callback_query(F.data == CANCEL_JOIN_CB)
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(callback, "Отменено. Отправьте /start, чтобы вернуться в меню.", menu_keyboard())


_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@router.message(JoinGroup.email, F.text)
async def got_email(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    email = (message.text or "").strip()
    if not valid_email(email):
        await message.answer(
            "Это не похоже на email, он выглядит так: name@example.com. Попробуйте ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return
    user = message.from_user
    assert user is not None
    # Блокировка на пользователя: два быстрых сообщения не добавят его дважды
    async with _locks[user.id]:
        if await state.get_state() != JoinGroup.email.state:
            return
        group_id = (await state.get_data())["group_id"]
        try:
            group = await db.join_group(group_id, member_entry(user, email.lower()))
        except AlreadyMember:
            await state.clear()
            await message.answer("Вы уже участник этой группы.", reply_markup=menu_keyboard())
            return
        except GroupUnavailable:
            await state.clear()
            await apologise(message, db, group_id)
            return
        await state.clear()
    await message.answer(
        joined_text(group, email.lower(), dates.today()), reply_markup=menu_keyboard()
    )
    await notify_owner(bot, group, user, email.lower())


async def apologise(message: Message, db: Database, group_id: str) -> None:
    group = await db.get_group(group_id)
    text = "😔 Пока вы вводили email, места закончились. Хотите создать новую группу?"
    if group is None:
        await message.answer("Такой группы не нашлось.", reply_markup=menu_keyboard())
        return
    await message.answer(text, reply_markup=create_offer(group["service_slug"]))


async def notify_owner(bot: Bot, group: Row, user: TgUser, email: str) -> None:
    """Организатору нужен email участника, чтобы отправить приглашение. Best effort."""
    owner = next((m for m in members_of(group) if m.get("role") == ROLE_OWNER), None)
    if owner is None or not owner.get("tg_id"):
        return  # группа создана не через бота: написать организатору некуда
    who = f"@{user.username}" if user.username else user.full_name
    text = (
        f"👤 Новый участник в группе {escape(title_of(group['service_slug']))}: {escape(who)}\n"
        f"Email для приглашения: {escape(email)}\n"
        f"Свободных мест: {free_slots(group)} из {group['max_slots']}"
    )
    try:
        await bot.send_message(owner["tg_id"], text)
    except TelegramAPIError:
        logger.warning("Не удалось уведомить организатора %s", owner["tg_id"], exc_info=True)


@router.message(JoinGroup.email)
async def email_not_text(message: Message) -> None:
    await message.answer("Отправьте email обычным текстом.", reply_markup=cancel_keyboard())
