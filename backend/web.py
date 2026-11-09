import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.auth import TelegramAuthError, verify_telegram_login
from backend.deps import check_csrf, csrf_token, optional_user, require_user
from shared.db.models import User
from shared.services import groups as groups_svc
from shared.services import memberships as ms
from shared.services.groups import PAGE_SIZE, GroupError
from shared.services.users import LANGUAGES, register_user, set_language

router = APIRouter()
CurrentUser = Annotated[User, Depends(require_user)]
Csrf = Depends(check_csrf)


def render(request: Request, name: str, user: User | None, status: int = 200, **ctx: object):
    flash = request.session.pop("flash", None)
    context = {"user": user, "csrf_token": csrf_token(request), "flash": flash, **ctx}
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status)


def back(url: str, request: Request, message: str | None = None) -> RedirectResponse:
    if message:
        request.session["flash"] = message
    return RedirectResponse(url, status_code=303)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user: Annotated[User | None, Depends(optional_user)]):
    return render(request, "home.html", user)


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, user: Annotated[User | None, Depends(optional_user)]):
    if user:
        return back("/cabinet", request)
    settings = request.app.state.settings
    base = settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return render(
        request,
        "login.html",
        None,
        bot_username=settings.bot_username,
        auth_url=f"{base}/auth/telegram",
    )


@router.get("/auth/telegram")
async def telegram_callback(request: Request):
    """Telegram Login Widget присылает подписанные поля GET-параметрами."""
    settings = request.app.state.settings
    try:
        profile = verify_telegram_login(dict(request.query_params), settings.bot_token)
    except TelegramAuthError as error:
        return render(
            request,
            "login.html",
            None,
            status=400,
            error=str(error),
            bot_username=settings.bot_username,
            auth_url=str(request.url_for("telegram_callback")),
        )
    # Находит по telegram_id или создаёт при первом входе
    user = await register_user(
        request.app.state.session_maker, profile.telegram_id, profile.username
    )
    if user.is_banned:
        return render(
            request,
            "login.html",
            None,
            status=403,
            error="Доступ ограничен.",
            bot_username=settings.bot_username,
            auth_url=str(request.url_for("telegram_callback")),
        )
    request.session.clear()  # новая сессия после входа: защита от фиксации сессии
    request.session["uid"] = user.id
    request.session["csrf"] = secrets.token_urlsafe(32)
    return back("/cabinet", request)


@router.post("/logout", dependencies=[Csrf])
async def logout(request: Request):
    request.session.clear()
    return back("/", request)


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, user: CurrentUser):
    entries = await ms.list_user_groups(request.app.state.session_maker, user.id)
    return render(request, "cabinet.html", user, entries=entries, limit=groups_svc.MAX_OPEN_GROUPS)


@router.get("/groups", response_class=HTMLResponse)
async def group_list(
    request: Request,
    user: CurrentUser,
    service: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
):
    sm = request.app.state.session_maker
    services = await groups_svc.list_active_services(sm)
    counts = await groups_svc.count_open_groups_by_service(sm, user.id)
    current = next((s for s in services if s.id == service), None)
    found = []
    if current:
        # Берём на одну запись больше, чтобы понять, есть ли следующая страница
        found = await groups_svc.list_open_groups(
            sm, current.id, user.id, (page - 1) * PAGE_SIZE, PAGE_SIZE + 1
        )
    return render(
        request,
        "groups.html",
        user,
        services=services,
        counts=counts,
        current=current,
        groups=found[:PAGE_SIZE],
        has_next=len(found) > PAGE_SIZE,
        page=page,
    )


@router.get("/groups/new", response_class=HTMLResponse)
async def group_new(request: Request, user: CurrentUser):
    services = await groups_svc.list_active_services(request.app.state.session_maker)
    return render(request, "group_new.html", user, services=services)


@router.post("/groups/new", dependencies=[Csrf])
async def group_create(
    request: Request,
    user: CurrentUser,
    service_id: Annotated[int, Form()],
    slots_total: Annotated[int, Form()],
    slots_free: Annotated[int, Form()],
    billing_day: Annotated[int, Form()],
):
    try:
        group = await groups_svc.create_group(
            request.app.state.session_maker,
            user.id,
            service_id,
            slots_total,
            slots_free,
            billing_day,
        )
    except GroupError as error:
        return back("/groups/new", request, str(error))
    return back(f"/groups/{group.id}", request, "Группа создана.")


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_card(request: Request, user: CurrentUser, group_id: int):
    card = await ms.get_group_card(request.app.state.session_maker, group_id, user.id)
    if card is None:
        return render(
            request,
            "error.html",
            user,
            status=404,
            title="Группа не найдена",
            text="Её нет, она закрыта или в ней нет свободных мест.",
        )
    return render(request, "group.html", user, card=card)


async def _action(request: Request, group_id: int, done: str, call) -> RedirectResponse:
    try:
        await call(request.app.state.session_maker)
    except GroupError as error:
        return back(f"/groups/{group_id}", request, str(error))
    return back(f"/groups/{group_id}", request, done)


@router.post("/groups/{group_id}/join", dependencies=[Csrf])
async def group_join(request: Request, user: CurrentUser, group_id: int):
    return await _action(
        request,
        group_id,
        "Заявка отправлена организатору.",
        lambda sm: groups_svc.request_join(sm, group_id, user.id),
    )


@router.post("/groups/{group_id}/leave", dependencies=[Csrf])
async def group_leave(request: Request, user: CurrentUser, group_id: int):
    try:
        await ms.leave_group(request.app.state.session_maker, group_id, user.id)
    except GroupError as error:
        return back(f"/groups/{group_id}", request, str(error))
    return back("/cabinet", request, "Вы вышли из группы.")


@router.post("/groups/{group_id}/close", dependencies=[Csrf])
async def group_close(request: Request, user: CurrentUser, group_id: int):
    try:
        await ms.close_group(request.app.state.session_maker, group_id, user.id)
    except GroupError as error:
        return back(f"/groups/{group_id}", request, str(error))
    return back("/cabinet", request, "Группа закрыта.")


@router.post("/groups/{group_id}/applications/{membership_id}", dependencies=[Csrf])
async def group_decide(
    request: Request,
    user: CurrentUser,
    group_id: int,
    membership_id: int,
    decision: Annotated[str, Form()],
):
    sm = request.app.state.session_maker
    approve = decision == "approve"
    done = "Заявка принята." if approve else "Заявка отклонена."
    call = groups_svc.approve_member if approve else groups_svc.reject_member
    return await _action(request, group_id, done, lambda _: call(sm, membership_id, user.id))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: CurrentUser):
    return render(request, "settings.html", user, languages=LANGUAGES)


@router.post("/settings", dependencies=[Csrf])
async def settings_save(request: Request, user: CurrentUser, language: Annotated[str, Form()]):
    if language not in LANGUAGES:
        return back("/settings", request, "Неизвестный язык.")
    await set_language(request.app.state.session_maker, user.id, language)
    return back("/settings", request, "Настройки сохранены.")
