from aiogram.types import InlineKeyboardMarkup
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
