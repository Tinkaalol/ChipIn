from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot import keyboards, ui
from bot.db.session import session_maker
from bot.handlers.my_groups import APPLICATION_DECIDE_PREFIX
from bot.services import groups as groups_svc
from bot.services import memberships as svc
from bot.services.users import register_user
from bot.texts import contact_html, money

router = Router(name="search")

SERVICE = "s:svc:"  # s:svc:<service_id>:<page>
GROUP = "s:grp:"  # s:grp:<group_id>:<page>
APPLY = "s:apply:"  # s:apply:<group_id>

NAV_HOME = ("Отмена", keyboards.HOME)


@router.callback_query(F.data == keyboards.FIND_GROUP)
async def choose_service(callback: CallbackQuery) -> None:
    services = await groups_svc.list_active_services(session_maker)
    rows = [[(f"{s.name} {s.plan_name}", f"{SERVICE}{s.id}:0")] for s in services]
    rows.append([("« Назад", keyboards.HOME), NAV_HOME])
    text = "Группу какого сервиса ищем?" if services else "Каталог сервисов пока пуст."
    await ui.show(callback, text, keyboards.build(rows))


@router.callback_query(F.data.startswith(SERVICE))
async def show_groups(callback: CallbackQuery) -> None:
    service_id, page = (int(x) for x in (callback.data or "").removeprefix(SERVICE).split(":"))
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    service = await groups_svc.get_service(session_maker, service_id)
    if service is None:
        await callback.answer("Этот сервис недоступен.", show_alert=True)
        return
    result = await svc.search_groups(session_maker, service_id, user.id, page)
    rows = [
        [
            (
                f"{money(g.price_per_member, service.currency)} · мест {g.slots_free}"
                f" · оплата {g.billing_day}-го",
                f"{GROUP}{g.id}:{result.page}",
            )
        ]
        for g in result.groups
    ]
    pager = []
    if result.page > 0:
        pager.append(("◀", f"{SERVICE}{service_id}:{result.page - 1}"))
    if result.page < result.pages - 1:
        pager.append(("▶", f"{SERVICE}{service_id}:{result.page + 1}"))
    if pager:
        rows.append(pager)
    rows.append([("« Назад", keyboards.FIND_GROUP), NAV_HOME])
    title = f"{service.name} {service.plan_name}"
    if result.groups:
        text = f"{title}: открытые группы (стр. {result.page + 1} из {result.pages})"
    else:
        text = f"{title}: пока нет открытых групп. Можно создать свою."
    await ui.show(callback, text, keyboards.build(rows))


@router.callback_query(F.data.startswith(GROUP))
async def show_card(callback: CallbackQuery) -> None:
    group_id, page = (int(x) for x in (callback.data or "").removeprefix(GROUP).split(":"))
    found = await svc.get_open_group(session_maker, group_id)
    if found is None:
        await callback.answer("Эта группа уже недоступна.", show_alert=True)
        return
    group, service = found
    text = (
        f"{service.name} {service.plan_name}\n"
        f"С человека: {money(group.price_per_member, service.currency)} в месяц\n"
        f"Свободных мест: {group.slots_free} из {group.slots_total}\n"
        f"Оплата: {group.billing_day}-го числа каждого месяца\n\n"
        "Контакт организатора откроется после того, как он примет вашу заявку."
    )
    rows = [
        [("Подать заявку", f"{APPLY}{group.id}")],
        [("« Назад", f"{SERVICE}{service.id}:{page}"), NAV_HOME],
    ]
    await ui.show(callback, text, keyboards.build(rows))


@router.callback_query(F.data.startswith(APPLY))
async def apply(callback: CallbackQuery, bot: Bot) -> None:
    group_id = int((callback.data or "").removeprefix(APPLY))
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    try:
        app = await svc.apply_to_group(session_maker, group_id, user.id)
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    decide = APPLICATION_DECIDE_PREFIX
    applicant = contact_html(app.applicant.telegram_id, app.applicant.username)
    await ui.notify(
        bot,
        app.owner.telegram_id,
        f"Новая заявка в группу {app.service.name} {app.service.plan_name}\n"
        f"От: {applicant}\n"
        f"Свободных мест: {app.group.slots_free}",
        keyboards.build(
            [
                [
                    ("Принять", f"{decide}ok:{app.membership.id}"),
                    ("Отклонить", f"{decide}no:{app.membership.id}"),
                ]
            ]
        ),
        html=True,
    )
    await ui.show(
        callback,
        "Заявка отправлена организатору. Я напишу, когда он ответит.",
        keyboards.build([[("В главное меню", keyboards.HOME)]]),
    )
