from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, Query
from pydantic import BaseModel

from shared.db.session import engine, session_maker
from shared.services import groups as groups_svc
from shared.services.groups import SessionMaker

# Публичные данные: контакты и состав участников через API не отдаются.
# Авторизация и действия пользователей добавятся позже, логика останется в shared.services.
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


def create_app(sm: SessionMaker = session_maker) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="Subscription sharing API", lifespan=lifespan)

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

    return app


app = create_app()
