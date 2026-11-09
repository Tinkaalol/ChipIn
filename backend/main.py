from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend import web
from backend.config import BackendSettings, get_backend_settings
from backend.deps import LoginRequired
from shared.db.session import engine, session_maker
from shared.formatting import money
from shared.services import groups as groups_svc
from shared.services.groups import SessionMaker

# JSON API отдаёт только публичные данные: контакты и состав участников в нём нет.
# Сайт (backend/web.py) работает через shared.services и проверяет роль на сервере.
BASE = Path(__file__).parent
SESSION_MAX_AGE = 14 * 24 * 3600
NO_VIEWER = 0


class ServiceOut(BaseModel):
    id: int
    name: str
    plan_name: str
    price_month: Decimal
    currency: str
    max_members: int


class GroupOut(BaseModel):
    id: int
    service_id: int
    price_per_member: Decimal
    slots_total: int
    slots_free: int
    billing_day: int


def create_app(
    sm: SessionMaker = session_maker, settings: BackendSettings | None = None
) -> FastAPI:
    settings = settings or get_backend_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="Subscription sharing", lifespan=lifespan)
    app.state.session_maker = sm
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=BASE / "templates")
    app.state.templates.env.filters["money"] = money
    # Подписанный cookie: HttpOnly всегда, Secure включён по умолчанию, SameSite=Lax
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def login_required(_: Request, __: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/services")
    async def services() -> list[ServiceOut]:
        found = await groups_svc.list_active_services(sm)
        return [ServiceOut.model_validate(s, from_attributes=True) for s in found]

    @app.get("/api/groups")
    async def open_groups(
        service_id: int,
        offset: int = Query(0, ge=0),
        limit: int = Query(groups_svc.PAGE_SIZE, ge=1, le=50),
    ) -> list[GroupOut]:
        found = await groups_svc.list_open_groups(sm, service_id, NO_VIEWER, offset, limit)
        return [GroupOut.model_validate(g, from_attributes=True) for g in found]

    app.include_router(web.router)
    return app


app = create_app()
