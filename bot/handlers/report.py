from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser

from bot import keyboards, ui
from bot.config import get_settings
from bot.db.session import session_maker
from bot.handlers.my_groups import render_group
from bot.services import groups as groups_svc
from bot.services import moderation as mod
from bot.services.users import register_user
from bot.texts import WELCOME

router = Router(name="report")

NEW = "r:new:"  # r:new:<group_id>:<user_id>
REASON = "r:reason:"
SKIP = "r:skip"
BACK = "r:back"
CANCEL = "r:cancel"
NAV = [("« Назад", BACK), ("Отмена", CANCEL)]


class ReportFlow(StatesGroup):
    reason = State()
    comment = State()


async def _show_reason(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.reason)
    rows = [[(label, f"{REASON}{key}")] for key, label in mod.REASONS.items()]
    rows.append(NAV)
    await ui.show(callback, "Что случилось?", keyboards.build(rows))


@router.callback_query(F.data.startswith(NEW))
async def start_report(callback: CallbackQuery, state: FSMContext) -> None:
    group_id, target_id = (int(x) for x in (callback.data or "").removeprefix(NEW).split(":"))
    await state.clear()
    await state.update_data(group_id=group_id, target_id=target_id)
    await _show_reason(callback, state)


@router.callback_query(F.data == CANCEL, StateFilter(ReportFlow))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ui.show(callback, WELCOME, keyboards.main_menu())


@router.callback_query(F.data == BACK, StateFilter(ReportFlow))
async def back(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == ReportFlow.comment.state:
        await _show_reason(callback, state)
        return
    group_id = (await state.get_data())["group_id"]
    await state.clear()
    await render_group(callback, group_id)


@router.callback_query(F.data.startswith(REASON), ReportFlow.reason)
async def pick_reason(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").removeprefix(REASON)
    if key not in mod.REASONS:
        await callback.answer("Выберите причину из списка.", show_alert=True)
        return
    await state.update_data(reason=key)
    await state.set_state(ReportFlow.comment)
    rows = [] if key == "other" else [[("Без комментария", SKIP)]]
    rows.append(NAV)
    text = f"Причина: {mod.REASONS[key]}.\nНапишите комментарий (до {mod.MAX_COMMENT} символов)."
    if key == "other":
        text = f"Опишите проблему (до {mod.MAX_COMMENT} символов)."
    await ui.show(callback, text, keyboards.build(rows))


async def _save(bot: Bot, state: FSMContext, tg_user: TgUser, comment: str) -> tuple[bool, str]:
    data = await state.get_data()
    user = await register_user(session_maker, tg_user.id, tg_user.username)
    try:
        report = await mod.create_report(
            session_maker, user.id, data["group_id"], data["target_id"], data["reason"], comment
        )
    except groups_svc.GroupError as e:
        return False, str(e)
    await state.clear()
    for admin_id in get_settings().admin_ids:
        await ui.notify(
            bot,
            admin_id,
            f"Новая жалоба #{report.id}: {report.reason[:100]}. Откройте /reports.",
        )
    return True, "Жалоба отправлена администраторам. Спасибо!"


@router.callback_query(F.data == SKIP, ReportFlow.comment)
async def skip_comment(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    done, text = await _save(bot, state, callback.from_user, "")
    markup = keyboards.home_keyboard() if done else keyboards.build([NAV])
    await ui.show(callback, text, markup)


@router.message(ReportFlow.comment, F.text)
async def got_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return
    done, text = await _save(bot, state, message.from_user, message.text or "")
    markup = keyboards.home_keyboard() if done else keyboards.build([NAV])
    await message.answer(text, reply_markup=markup)
