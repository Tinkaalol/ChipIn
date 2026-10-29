from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards
from bot.db.session import session_maker
from bot.services import groups as svc
from bot.services.users import register_user
from bot.texts import WELCOME

router = Router(name="create_group")


class CreateGroup(StatesGroup):
    service = State()
    tariff = State()
    total = State()
    free = State()
    day = State()
    confirm = State()


# Порядок шагов нужен для кнопки «Назад»
STEPS: list[State] = [
    CreateGroup.service,
    CreateGroup.tariff,
    CreateGroup.total,
    CreateGroup.free,
    CreateGroup.day,
    CreateGroup.confirm,
]

CURRENCY_SIGNS = {"RUB": "₽"}
DAYS_PER_ROW = 7


def money(amount: Decimal | str, currency: str) -> str:
    return f"{Decimal(amount):.2f} {CURRENCY_SIGNS.get(currency, currency)}"


async def _render(step: State, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура для шага по данным из FSM."""
    if step == CreateGroup.service:
        services = await svc.list_active_services(session_maker)
        if not services:
            return "Каталог сервисов пока пуст.", keyboards.services_keyboard([])
        items = [
            (s.id, f"{s.name} {s.plan_name} · {money(s.price_month, s.currency)}") for s in services
        ]
        return "Для какого сервиса создаём группу?", keyboards.services_keyboard(items)
    cur = data.get("currency", "RUB")
    if step == CreateGroup.tariff:
        text = (
            f"{data['service_name']}\n"
            f"Тариф: {data['plan_name']}\n"
            f"Цена в месяц: {money(data['price'], cur)}\n"
            f"Максимум мест на подписке: {data['max_members']}\n\n"
            "Тариф и цену задаёт сервис, изменить их нельзя."
        )
        return text, keyboards.next_keyboard()
    if step == CreateGroup.total:
        text = "Сколько всего мест на подписке, считая вас?"
        return text, keyboards.numbers_keyboard(
            keyboards.CG_TOTAL, range(2, data["max_members"] + 1), per_row=3
        )
    if step == CreateGroup.free:
        text = f"Сколько мест из {data['total']} свободно? Одно занимаете вы."
        return text, keyboards.numbers_keyboard(
            keyboards.CG_FREE, range(1, data["total"]), per_row=3
        )
    if step == CreateGroup.day:
        text = "Какого числа месяца оплата?"
        return text, keyboards.numbers_keyboard(
            keyboards.CG_DAY, range(svc.BILLING_DAY_MIN, svc.BILLING_DAY_MAX + 1), DAYS_PER_ROW
        )
    per_member = svc.calc_price_per_member(Decimal(data["price"]), data["total"])
    text = (
        "Проверьте группу:\n\n"
        f"Сервис: {data['service_name']} {data['plan_name']}\n"
        f"Цена в месяц: {money(data['price'], cur)}\n"
        f"Мест всего: {data['total']}, свободно: {data['free']}\n"
        f"День оплаты: {data['day']}-е число\n"
        f"С каждого участника: {money(per_member, cur)} в месяц\n\n"
        "Деньги бот не принимает: участники переводят их вам сами."
    )
    return text, keyboards.confirm_keyboard()


async def _go(callback: CallbackQuery, state: FSMContext, step: State) -> None:
    await state.set_state(step)
    text, markup = await _render(step, await state.get_data())
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def _to_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(WELCOME, reply_markup=keyboards.main_menu())
    await callback.answer()


def _value(callback: CallbackQuery, prefix: str) -> int:
    return int((callback.data or "").removeprefix(prefix))


@router.callback_query(F.data == keyboards.CREATE_GROUP)
async def start_flow(callback: CallbackQuery, state: FSMContext) -> None:
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    try:
        await svc.ensure_can_create(session_maker, user.id)
    except svc.GroupLimitError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.clear()
    await state.update_data(owner_id=user.id)
    await _go(callback, state, CreateGroup.service)


@router.callback_query(F.data == keyboards.CG_CANCEL, StateFilter(*STEPS))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _to_start(callback, state)


@router.callback_query(F.data == keyboards.CG_BACK, StateFilter(*STEPS))
async def back(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    index = next(i for i, s in enumerate(STEPS) if s.state == current)
    if index == 0:
        await _to_start(callback, state)
        return
    await _go(callback, state, STEPS[index - 1])


@router.callback_query(F.data.startswith(keyboards.CG_SERVICE), CreateGroup.service)
async def pick_service(callback: CallbackQuery, state: FSMContext) -> None:
    service = await svc.get_service(session_maker, _value(callback, keyboards.CG_SERVICE))
    if service is None or not service.is_active:
        await callback.answer("Этот сервис недоступен.", show_alert=True)
        return
    await state.update_data(
        service_id=service.id,
        service_name=service.name,
        plan_name=service.plan_name,
        price=str(service.price_month),
        currency=service.currency,
        max_members=service.max_members,
    )
    await _go(callback, state, CreateGroup.tariff)


@router.callback_query(F.data == keyboards.CG_NEXT, CreateGroup.tariff)
async def accept_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    await _go(callback, state, CreateGroup.total)


@router.callback_query(F.data.startswith(keyboards.CG_TOTAL), CreateGroup.total)
async def pick_total(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        total = svc.validate_slots_total(_value(callback, keyboards.CG_TOTAL), data["max_members"])
    except svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.update_data(total=total)
    await _go(callback, state, CreateGroup.free)


@router.callback_query(F.data.startswith(keyboards.CG_FREE), CreateGroup.free)
async def pick_free(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        free = svc.validate_slots_free(_value(callback, keyboards.CG_FREE), data["total"])
    except svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.update_data(free=free)
    await _go(callback, state, CreateGroup.day)


@router.callback_query(F.data.startswith(keyboards.CG_DAY), CreateGroup.day)
async def pick_day(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        day = svc.validate_billing_day(_value(callback, keyboards.CG_DAY))
    except svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.update_data(day=day)
    await _go(callback, state, CreateGroup.confirm)


@router.callback_query(F.data == keyboards.CG_CONFIRM, CreateGroup.confirm)
async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        group = await svc.create_group(
            session_maker,
            owner_id=data["owner_id"],
            service_id=data["service_id"],
            slots_total=data["total"],
            slots_free=data["free"],
            billing_day=data["day"],
        )
    except svc.GroupError as e:
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(str(e), reply_markup=keyboards.main_menu())
        await callback.answer()
        return
    await state.clear()
    text = (
        f"Группа создана. С каждого участника {money(group.price_per_member, data['currency'])} "
        "в месяц. Она появилась в разделе «Мои группы»."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboards.main_menu())
    await callback.answer()
