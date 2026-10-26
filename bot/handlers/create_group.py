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

router = Router(name="create_group")


class CreateGroup(StatesGroup):
    service = State()
    price = State()
    total = State()
    free = State()
    day = State()
    confirm = State()


# Порядок шагов нужен для кнопки «Назад»
STEPS: list[State] = [
    CreateGroup.service,
    CreateGroup.price,
    CreateGroup.total,
    CreateGroup.free,
    CreateGroup.day,
    CreateGroup.confirm,
]

CURRENCY_SIGNS = {"RUB": "₽"}


def money(amount: Decimal | str, currency: str) -> str:
    return f"{Decimal(amount):.2f} {CURRENCY_SIGNS.get(currency, currency)}"


async def _render(step: State, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура для шага по данным из FSM."""
    if step == CreateGroup.service:
        services = await svc.list_active_services(session_maker)
        items = [(s.id, f"{s.name} {s.plan_name}") for s in services]
        return "Для какого сервиса создаём группу?", keyboards.services_keyboard(items)
    cur = data.get("currency", "RUB")
    if step == CreateGroup.price:
        label = f"Оставить {money(data['catalog_price'], cur)}"
        text = (
            f"{data['service_name']}, тариф «{data['plan_name']}».\n"
            f"В каталоге цена в месяц: {money(data['catalog_price'], cur)}.\n"
            "Оставьте её или напишите свою, например 349 или 349.50."
        )
        return text, keyboards.price_keyboard(label)
    if step == CreateGroup.total:
        text = f"Сколько всего мест в группе, считая вас? От 2 до {data['max_members']}."
        return text, keyboards.nav_keyboard()
    if step == CreateGroup.free:
        text = f"Сколько мест свободно? От 1 до {data['total'] - 1}: одно место занимаете вы."
        return text, keyboards.nav_keyboard()
    if step == CreateGroup.day:
        text = (
            f"Какого числа месяца оплата? От {svc.BILLING_DAY_MIN} до "
            f"{svc.BILLING_DAY_MAX}, чтобы дата была в любом месяце."
        )
        return text, keyboards.nav_keyboard()
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


async def _go(target: Message, state: FSMContext, step: State, *, edit: bool) -> None:
    await state.set_state(step)
    text, markup = await _render(step, await state.get_data())
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _finish(callback: CallbackQuery, state: FSMContext, text: str) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboards.main_menu())
    await callback.answer()


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
    if isinstance(callback.message, Message):
        await _go(callback.message, state, CreateGroup.service, edit=True)
    await callback.answer()


@router.callback_query(F.data == keyboards.CG_CANCEL, StateFilter(*STEPS))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _finish(callback, state, "Создание группы отменено.")


@router.callback_query(F.data == keyboards.CG_BACK, StateFilter(*STEPS))
async def back(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    index = next(i for i, s in enumerate(STEPS) if s.state == current)
    if index == 0:
        await _finish(callback, state, "Главное меню.")
        return
    if isinstance(callback.message, Message):
        await _go(callback.message, state, STEPS[index - 1], edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CG_SERVICE), CreateGroup.service)
async def pick_service(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = int((callback.data or "").removeprefix(keyboards.CG_SERVICE))
    service = await svc.get_service(session_maker, service_id)
    if service is None or not service.is_active:
        await callback.answer("Этот сервис недоступен.", show_alert=True)
        return
    await state.update_data(
        service_id=service.id,
        service_name=service.name,
        plan_name=service.plan_name,
        catalog_price=str(service.price_month),
        currency=service.currency,
        max_members=service.max_members,
    )
    if isinstance(callback.message, Message):
        await _go(callback.message, state, CreateGroup.price, edit=True)
    await callback.answer()


@router.callback_query(F.data == keyboards.CG_PRICE_DEFAULT, CreateGroup.price)
async def default_price(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(price=data["catalog_price"])
    if isinstance(callback.message, Message):
        await _go(callback.message, state, CreateGroup.total, edit=True)
    await callback.answer()


@router.message(CreateGroup.price, F.text)
async def input_price(message: Message, state: FSMContext) -> None:
    try:
        price = svc.parse_price(message.text or "")
    except svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.nav_keyboard())
        return
    await state.update_data(price=str(price))
    await _go(message, state, CreateGroup.total, edit=False)


@router.message(CreateGroup.total, F.text)
async def input_total(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        total = svc.validate_slots_total(svc.parse_int(message.text or ""), data["max_members"])
    except svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.nav_keyboard())
        return
    await state.update_data(total=total)
    await _go(message, state, CreateGroup.free, edit=False)


@router.message(CreateGroup.free, F.text)
async def input_free(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        free = svc.validate_slots_free(svc.parse_int(message.text or ""), data["total"])
    except svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.nav_keyboard())
        return
    await state.update_data(free=free)
    await _go(message, state, CreateGroup.day, edit=False)


@router.message(CreateGroup.day, F.text)
async def input_day(message: Message, state: FSMContext) -> None:
    try:
        day = svc.validate_billing_day(svc.parse_int(message.text or ""))
    except svc.GroupError as e:
        await message.answer(str(e), reply_markup=keyboards.nav_keyboard())
        return
    await state.update_data(day=day)
    await _go(message, state, CreateGroup.confirm, edit=False)


@router.callback_query(F.data == keyboards.CG_CONFIRM, CreateGroup.confirm)
async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        group = await svc.create_group(
            session_maker,
            owner_id=data["owner_id"],
            service_id=data["service_id"],
            price_month=Decimal(data["price"]),
            slots_total=data["total"],
            slots_free=data["free"],
            billing_day=data["day"],
        )
    except svc.GroupError as e:
        await _finish(callback, state, str(e))
        return
    cur = data["currency"]
    await _finish(
        callback,
        state,
        f"Группа создана. С каждого участника {money(group.price_per_member, cur)} в месяц.",
    )
