"""Заявка на оплату: отправка админу, подтверждение и отклонение."""

from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import APP_FAILED, APP_PENDING, Database, Row

APP_OK_PREFIX = "adm_ok_"
APP_NO_PREFIX = "adm_no_"

USER_ACCEPTED = (
    "Заявка принята! Проверяем перевод через Kaspi, обычно это занимает до 15 минут. "
    "Как только подтвердим — пришлём приглашение на почту."
)


class SubmitError(Exception):
    """Не удалось передать заявку администратору."""


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₸"


def telegram_label(app: Row) -> str:
    """Для организатора важнее @username; без него остаются имя и ID."""
    if app.get("tg_username"):
        return f"{escape(app['tg_username'])} (ID {app['tg_id']})"
    name = escape(app.get("tg_name") or "профиль")
    link = f'<a href="tg://user?id={app["tg_id"]}">{name}</a>'
    return f"без username, {link} (ID {app['tg_id']})"


def creator_username(app: Row) -> str:
    """Что писать в groups.creator_tg_username, если у человека нет @username."""
    return app["tg_username"] or f"tg_id:{app['tg_id']}"


def admin_text(app: Row, service: Row, group: Row | None) -> str:
    if app["action"] == "create":
        kind = "Создание новой группы"
    else:
        taken = (
            f"занято {group['current_slots']} из {group['max_slots']}"
            if group
            else "группа не найдена"
        )
        kind = f"Присоединение к группе {escape(str(app['group_id']))} ({taken})"
    return (
        "<b>Новая заявка на оплату</b>\n"
        f"Тип: {kind}\n"
        f"Сервис: {escape(service['title'])}\n"
        f"Сумма: <b>{money(app['amount_kzt'])}</b>\n"
        f"Telegram: {telegram_label(app)}\n"
        f"Email для инвайта: {escape(app['email'])}"
    )


def admin_keyboard(app_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату", callback_data=f"{APP_OK_PREFIX}{app_id}"
                )
            ],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{APP_NO_PREFIX}{app_id}")],
        ]
    )


async def submit_application(
    bot: Bot, db: Database, admin_id: int, data: dict, service: Row
) -> Row:
    """Сохраняет заявку (pending) и отправляет её админу. При сбое заявка помечается failed."""
    app = await db.create_application(
        {
            "action": data["action"],
            "service_slug": data["service_slug"],
            "group_id": data.get("group_id"),
            "email": data["email"],
            "tg_username": data.get("username"),
            "tg_id": data["tg_id"],
            "tg_name": data.get("name"),
            "amount_kzt": service["price_per_person_kzt"],
            "status": APP_PENDING,
        }
    )
    group = await db.get_group(app["group_id"]) if app["group_id"] else None
    try:
        await bot.send_message(
            admin_id, admin_text(app, service, group), reply_markup=admin_keyboard(app["id"])
        )
    except TelegramAPIError as error:
        await db.set_application_status(app["id"], APP_FAILED)
        raise SubmitError(str(error)) from error
    return app


async def apply_approval(db: Database, app: Row) -> Row:
    """Меняет таблицу groups. Бросает GroupUnavailable, если места в группе уже нет."""
    if app["action"] == "create":
        service = await db.get_service(app["service_slug"])
        if service is None:
            raise LookupError(f"Сервис {app['service_slug']} не найден")
        return await db.create_group(
            app["service_slug"], creator_username(app), app["email"], int(service["total_slots"])
        )
    return await db.join_group(app["group_id"])


def confirmed_text(email: str) -> str:
    return f"Оплата подтверждена! Вы в группе. Ожидайте приглашение на {escape(email)}."


def rejected_text(contact: str) -> str:
    return (
        "Мы не нашли ваш перевод. Если вы уже заплатили, свяжитесь с нами и пришлите чек: "
        f"{contact}"
    )
