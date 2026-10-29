from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

FIND_GROUP = "menu:find"
CREATE_GROUP = "menu:create"
MY_GROUPS = "menu:my"
HOME = "menu:home"

CG_BACK = "cg:back"
CG_CANCEL = "cg:cancel"
CG_SERVICE = "cg:svc:"
CG_NEXT = "cg:next"
CG_TOTAL = "cg:total:"
CG_FREE = "cg:free:"
CG_DAY = "cg:day:"
CG_CONFIRM = "cg:confirm"


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Найти группу", callback_data=FIND_GROUP)
    builder.button(text="Создать группу", callback_data=CREATE_GROUP)
    builder.button(text="Мои группы", callback_data=MY_GROUPS)
    builder.adjust(1)
    return builder.as_markup()


def home_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="В главное меню", callback_data=HOME)
    return builder.as_markup()


def _nav_row(builder: InlineKeyboardBuilder) -> None:
    """«Назад» — на шаг выше, «Отмена» — в начало бота."""
    builder.row(
        InlineKeyboardButton(text="« Назад", callback_data=CG_BACK),
        InlineKeyboardButton(text="Отмена", callback_data=CG_CANCEL),
    )


def services_keyboard(services: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for service_id, title in services:
        builder.button(text=title, callback_data=f"{CG_SERVICE}{service_id}")
    builder.adjust(1)
    _nav_row(builder)
    return builder.as_markup()


def next_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Продолжить", callback_data=CG_NEXT)
    _nav_row(builder)
    return builder.as_markup()


def numbers_keyboard(prefix: str, values: Iterable[int], per_row: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in values:
        builder.button(text=str(value), callback_data=f"{prefix}{value}")
    builder.adjust(per_row)
    _nav_row(builder)
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать группу", callback_data=CG_CONFIRM)
    _nav_row(builder)
    return builder.as_markup()
