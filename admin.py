"""Кнопки администратора под заявкой: подтвердить оплату или отклонить."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from db import APP_APPROVED, APP_PENDING, APP_REJECTED, Database, GroupUnavailable
from payments import (
    APP_NO_PREFIX,
    APP_OK_PREFIX,
    apply_approval,
    confirmed_text,
    rejected_text,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")

NO_ACCESS = "Нет доступа."
ALREADY_DONE = "Заявка уже обработана или не найдена."


def is_admin(user_id: int, admin_id: int) -> bool:
    return admin_id != 0 and user_id == admin_id


def contact_for_users(support_contact: str, admin_id: int) -> str:
    if support_contact:
        return escape(support_contact)
    return f'<a href="tg://user?id={admin_id}">администратор</a>'


async def _finish_admin_message(callback: CallbackQuery, status: str, extra: str = "") -> None:
    """Убирает кнопки и дописывает итог к сообщению админа."""
    if isinstance(callback.message, Message):
        text = callback.message.html_text + f"\n\n<b>{status}</b>" + extra
        await callback.message.edit_text(text, reply_markup=None)
    await callback.answer()


async def _tell_user(bot: Bot, tg_id: int, text: str) -> str:
    """Возвращает пометку для админа, если пользователю не удалось написать."""
    try:
        await bot.send_message(tg_id, text)
    except TelegramAPIError:
        logger.warning("Не удалось написать пользователю %s", tg_id, exc_info=True)
        return "\n⚠️ Не удалось написать пользователю: свяжитесь с ним сами."
    return ""


@router.callback_query(F.data.startswith(APP_OK_PREFIX))
async def approve(callback: CallbackQuery, bot: Bot, db: Database, admin_id: int) -> None:
    if not is_admin(callback.from_user.id, admin_id):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    app_id = (callback.data or "").removeprefix(APP_OK_PREFIX)
    # Заявку занимаем первой: двойной клик или второй админ не изменят groups дважды
    app = await db.claim_application(app_id, APP_APPROVED)
    if app is None:
        await callback.answer(ALREADY_DONE, show_alert=True)
        return
    try:
        await apply_approval(db, app)
    except GroupUnavailable:
        await db.set_application_status(app_id, APP_PENDING)
        await callback.answer(
            "В группе уже нет мест. Заявка осталась в ожидании: отклоните её и верните деньги.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Не удалось применить заявку %s", app_id)
        await db.set_application_status(app_id, APP_PENDING)
        await callback.answer("Ошибка при обновлении групп. Попробуйте ещё раз.", show_alert=True)
        return
    note = await _tell_user(bot, app["tg_id"], confirmed_text(app["email"]))
    await _finish_admin_message(callback, "Подтверждено ✅", note)


@router.callback_query(F.data.startswith(APP_NO_PREFIX))
async def reject(
    callback: CallbackQuery, bot: Bot, db: Database, admin_id: int, support_contact: str
) -> None:
    if not is_admin(callback.from_user.id, admin_id):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    app_id = (callback.data or "").removeprefix(APP_NO_PREFIX)
    app = await db.claim_application(app_id, APP_REJECTED)
    if app is None:
        await callback.answer(ALREADY_DONE, show_alert=True)
        return
    text = rejected_text(contact_for_users(support_contact, admin_id))
    note = await _tell_user(bot, app["tg_id"], text)
    await _finish_admin_message(callback, "Отклонено ❌", note)
