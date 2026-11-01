from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot import keyboards, ui
from bot.db.session import session_maker
from bot.handlers.my_groups import APPLICATION_DECIDE_PREFIX
from bot.services import groups as groups_svc
from bot.services import memberships as svc
from bot.services.ratelimit import RATE_LIMIT_MESSAGE, application_limiter
from bot.services.users import register_user
from bot.texts import contact_html, money

router = Router(name="search")

SERVICE = "s:svc:"  # s:svc:<service_id>:<page>
GROUP = "s:grp:"  # s:grp:<group_id>:<page>
APPLY = "s:apply:"  # s:apply:<group_id>

NAV_HOME = ("Отмена", keyboards.HOME)


@router.callback_query(F.data == keyboards.FIND_GROUP)
async def choose_service(callback: CallbackQuery) -> None:
    user = await register_user(session_maker, callback.from_user.id, callback.from_user.username)
    services = await groups_svc.list_active_services(session_maker)
    counts = await groups_svc.count_open_groups_by_service(session_maker, user.id)
    rows = [
        [(f"{s.name} {s.plan_name} ({counts.get(s.id, 0)})", f"{SERVICE}{s.id}:0")]
        for s in services
    ]
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
    total = (await groups_svc.count_open_groups_by_service(session_maker, user.id)).get(
        service_id, 0
    )
    pages = max(1, -(-total // groups_svc.PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    groups = await groups_svc.list_open_groups(
        session_maker, service_id, user.id, page * groups_svc.PAGE_SIZE, groups_svc.PAGE_SIZE
    )
    nav = [("« К сервисам", keyboards.FIND_GROUP), NAV_HOME]
    if not groups:
        rows = [[("Создать свою группу", keyboards.CREATE_GROUP)], nav]
        await ui.show(callback, "Пока нет групп", keyboards.build(rows))
        return
    rows = [
        [
            (
                f"{money(g.price_per_member, service.currency)} · {g.slots_free} из "
                f"{g.slots_total} мест · оплата {g.billing_day}-го",
                f"{GROUP}{g.id}:{page}",
            )
        ]
        for g in groups
    ]
    pager = []
    if page > 0:
        pager.append(("◀ Назад", f"{SERVICE}{service_id}:{page - 1}"))
    if page < pages - 1:
        pager.append(("Далее ▶", f"{SERVICE}{service_id}:{page + 1}"))
    if pager:
        rows.append(pager)
    rows.append(nav)
    title = f"{service.name} {service.plan_name}: открытые группы (стр. {page + 1} из {pages})"
    await ui.show(callback, title, keyboards.build(rows))


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
    if not application_limiter.allow(user.id):
        await callback.answer(RATE_LIMIT_MESSAGE, show_alert=True)
        return
    try:
        app = await svc.apply_to_group(session_maker, group_id, user.id)
    except groups_svc.GroupError as e:
        await callback.answer(str(e), show_alert=True)
        return
    decide = APPLICATION_DECIDE_PREFIX
    applicant = contact_html(app.applicant.telegram_id, app.applicant.username)
    delivered = await ui.notify(
        bot,
        app.owner.telegram_id,
        f"Новая заявка в группу {escape(app.service.name)} {escape(app.service.plan_name)}\n"
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
    text = "Заявка отправлена организатору. Я напишу, когда он ответит."
    if not delivered:
        text = (
            "Заявка сохранена, но организатор пока не получил уведомление "
            "(возможно, он остановил бота). Она появится у него, когда он вернётся."
        )
    await ui.show(
        callback,
        text,
        keyboards.build([[("В главное меню", keyboards.HOME)]]),
    )
