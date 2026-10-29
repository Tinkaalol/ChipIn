from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import keyboards
from bot.db.models import Group, GroupStatus, Service
from bot.db.session import session_maker
from bot.services import groups as svc
from bot.services.users import register_user

router = Router(name="my_groups")

STATUS_LABELS = {
    GroupStatus.OPEN: "идёт набор",
    GroupStatus.FULL: "мест нет",
    GroupStatus.CLOSED: "закрыта",
}


def format_group(group: Group, service: Service) -> str:
    return (
        f"{service.name} {service.plan_name} — {STATUS_LABELS[group.status]}\n"
        f"Свободно мест: {group.slots_free} из {group.slots_total}, "
        f"с участника {group.price_per_member:.2f} ₽, оплата {group.billing_day}-го числа"
    )


@router.callback_query(F.data == keyboards.MY_GROUPS)
async def show_my_groups(callback: CallbackQuery) -> None:
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    rows = await svc.list_owned_groups(session_maker, user.id)
    if rows:
        text = "Ваши группы:\n\n" + "\n\n".join(format_group(g, s) for g, s in rows)
    else:
        text = "У вас пока нет групп. Создайте первую кнопкой «Создать группу»."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboards.home_keyboard())
    await callback.answer()
