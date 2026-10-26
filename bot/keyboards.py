from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

FIND_GROUP = "menu:find"
CREATE_GROUP = "menu:create"
MY_GROUPS = "menu:my"


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Найти группу", callback_data=FIND_GROUP)
    builder.button(text="Создать группу", callback_data=CREATE_GROUP)
    builder.button(text="Мои группы", callback_data=MY_GROUPS)
    builder.adjust(1)
    return builder.as_markup()


CG_BACK = "cg:back"
CG_CANCEL = "cg:cancel"
CG_SERVICE = "cg:svc:"
CG_PRICE_DEFAULT = "cg:price_default"
CG_CONFIRM = "cg:confirm"


def nav_row(builder: InlineKeyboardBuilder) -> None:
    """Добавляет в конец клавиатуры строку «Назад» / «Отмена»."""
    builder.row(
        InlineKeyboardButton(text="« Назад", callback_data=CG_BACK),
        InlineKeyboardButton(text="Отмена", callback_data=CG_CANCEL),
    )


def services_keyboard(services: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for service_id, title in services:
        builder.button(text=title, callback_data=f"{CG_SERVICE}{service_id}")
    builder.adjust(1)
    nav_row(builder)
    return builder.as_markup()


def price_keyboard(default_label: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=default_label, callback_data=CG_PRICE_DEFAULT)
    nav_row(builder)
    return builder.as_markup()


def nav_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav_row(builder)
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать группу", callback_data=CG_CONFIRM)
    nav_row(builder)
    return builder.as_markup()
