"""Заявка на создание группы или вступление: deep-links с сайта, ввод email, итоговая карточка."""

import re
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser

from db import Database, Row, is_joinable
from handlers import CREATE_PREFIX, JOIN_PREFIX, SERVICES_CB, button, money

router = Router(name="flow")

PAID_CB = "paid"
CANCEL_CB = "cancel_flow"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ACTION_CREATE = "create"
ACTION_JOIN = "join"


class Application(StatesGroup):
    email = State()
    confirm = State()


def valid_email(text: str) -> bool:
    return EMAIL_RE.fullmatch(text.strip()) is not None


def applicant_info(user: TgUser) -> dict[str, str | int | None]:
    """@username, а если его нет, то tg_id и имя: по ним организатор найдёт человека."""
    return {
        "username": f"@{user.username}" if user.username else None,
        "tg_id": user.id,
        "name": user.full_name,
    }


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[button("✖ Отмена", CANCEL_CB)])


def email_prompt(service: Row, action: str) -> str:
    what = "создать новую группу" if action == ACTION_CREATE else "вступить в группу"
    return (
        f"Вы хотите {what}: <b>{escape(service['title'])}</b>.\n\n"
        "Введите ваш личный email, на него придёт официальное приглашение от сервиса.\n"
        "🔒 Чужие пароли мы не используем и не просим: доступ выдаётся на ваш личный аккаунт. "
        "Пароли и данные карт в чат не отправляйте."
    )


def summary_text(data: dict, service: Row, kaspi_info: str) -> str:
    action = "Новая группа" if data["action"] == ACTION_CREATE else "Вступление в группу"
    requisites = escape(kaspi_info) if kaspi_info else "реквизиты уточните у администратора"
    return (
        f"<b>Ваша заявка</b>\n"
        f"{action}: {escape(service['title'])}\n"
        f"Ваш email: {escape(data['email'])}\n\n"
        f"К оплате: <b>{money(service['price_per_person_kzt'])}</b> в месяц\n"
        f"Реквизиты для перевода:\n{requisites}\n\n"
        "После перевода нажмите «Я оплатил»."
    )


async def _reply(target: Message | CallbackQuery, text: str, markup=None) -> None:
    message = target if isinstance(target, Message) else target.message
    if isinstance(target, CallbackQuery):
        await target.answer()
    if isinstance(message, Message):
        await message.answer(text, reply_markup=markup)


async def begin_create(
    target: Message | CallbackQuery, state: FSMContext, db: Database, slug: str, user: TgUser
) -> None:
    service = await db.get_service(slug)
    if service is None:
        await state.clear()
        await _reply(target, "Этот сервис недоступен. Выберите другой:", _services_button())
        return
    await _ask_email(target, state, service, ACTION_CREATE, user, service_slug=slug)


async def begin_join(
    target: Message | CallbackQuery, state: FSMContext, db: Database, group_id: str, user: TgUser
) -> None:
    await state.clear()
    group = await db.get_group(group_id)
    if group is None:
        await _reply(target, "Такой группы не нашлось. Посмотрите другие:", _services_button())
        return
    slug = group["service_slug"]
    if user.username and group.get("creator_tg_username", "").lower().lstrip("@") == (
        user.username.lower()
    ):
        await _reply(target, "Это ваша собственная группа, вступать в неё не нужно.")
        return
    if not is_joinable(group):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                button("➕ Создать новую группу", f"{CREATE_PREFIX}{slug}"),
                button("‹ К сервисам", SERVICES_CB),
            ]
        )
        text = "😔 К сожалению, в этой группе уже нет мест. Хотите создать новую для этого сервиса?"
        await _reply(target, text, markup)
        return
    service = await db.get_service(slug)
    if service is None:
        await _reply(target, "Этот сервис недоступен. Выберите другой:", _services_button())
        return
    await _ask_email(
        target, state, service, ACTION_JOIN, user, service_slug=slug, group_id=group_id
    )


async def _ask_email(
    target: Message | CallbackQuery,
    state: FSMContext,
    service: Row,
    action: str,
    user: TgUser,
    **ids: str,
) -> None:
    await state.clear()
    await state.set_state(Application.email)
    await state.update_data(action=action, **ids, **applicant_info(user))
    await _reply(target, email_prompt(service, action), cancel_keyboard())


def _services_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[button("‹ К сервисам", SERVICES_CB)])


# --- точки входа: ссылки с сайта и кнопки в боте. Они выше обработчика email, чтобы ссылка
# --- сработала и посреди незаконченной заявки


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(CREATE_PREFIX)))
async def start_create_link(
    message: Message, command: CommandObject, state: FSMContext, db: Database
) -> None:
    slug = (command.args or "").removeprefix(CREATE_PREFIX)
    await begin_create(message, state, db, slug, message.from_user)  # type: ignore[arg-type]


@router.message(CommandStart(deep_link=True, magic=F.args.startswith(JOIN_PREFIX)))
async def start_join_link(
    message: Message, command: CommandObject, state: FSMContext, db: Database
) -> None:
    group_id = (command.args or "").removeprefix(JOIN_PREFIX)
    await begin_join(message, state, db, group_id, message.from_user)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith(CREATE_PREFIX))
async def create_button(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    slug = (callback.data or "").removeprefix(CREATE_PREFIX)
    await begin_create(callback, state, db, slug, callback.from_user)


@router.callback_query(F.data.startswith(JOIN_PREFIX))
async def join_button(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    group_id = (callback.data or "").removeprefix(JOIN_PREFIX)
    await begin_join(callback, state, db, group_id, callback.from_user)


@router.callback_query(F.data == CANCEL_CB)
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _reply(callback, "Заявка отменена. Отправьте /start, чтобы начать заново.")


# --- ввод email и итоговая карточка


@router.message(Application.email, F.text)
async def got_email(message: Message, state: FSMContext, db: Database, kaspi_info: str) -> None:
    email = (message.text or "").strip()
    if not valid_email(email):
        await message.answer(
            "Это не похоже на email, он выглядит так: name@example.com. Попробуйте ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return
    await state.update_data(email=email.lower())
    data = await state.get_data()
    service = await db.get_service(data["service_slug"])
    if service is None:
        await state.clear()
        await message.answer("Этот сервис больше недоступен.", reply_markup=_services_button())
        return
    await state.set_state(Application.confirm)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[button("✅ Я оплатил", PAID_CB), button("✖ Отмена", CANCEL_CB)]
    )
    await message.answer(summary_text(data, service, kaspi_info), reply_markup=markup)


@router.message(StateFilter(Application.email))
async def email_not_text(message: Message) -> None:
    await message.answer("Отправьте email обычным текстом.", reply_markup=cancel_keyboard())


@router.callback_query(Application.confirm, F.data == PAID_CB)
async def paid(callback: CallbackQuery) -> None:
    # Проверка оплаты и запись заявки в Supabase пока не реализованы
    await callback.answer("Следующий шаг (подтверждение оплаты) ещё в разработке.", show_alert=True)
