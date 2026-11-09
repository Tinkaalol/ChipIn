from decimal import Decimal

import httpx

from backend.main import create_app
from shared.db.models import Group, Service, User


async def test_api_lists_services_and_open_groups_without_contacts(session_maker) -> None:
    async with session_maker() as session:
        owner = User(telegram_id=1, username="secret_owner")
        service = Service(
            name="Netflix", plan_name="Premium", price_month=Decimal("4000"), max_members=4
        )
        session.add_all([owner, service])
        await session.flush()
        session.add(
            Group(
                service_id=service.id,
                owner_id=owner.id,
                price_per_member=Decimal("1000"),
                slots_total=4,
                slots_free=3,
                billing_day=5,
            )
        )
        await session.commit()
        service_id = service.id

    transport = httpx.ASGITransport(app=create_app(session_maker))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        services = (await client.get("/api/services")).json()
        groups = (await client.get("/api/groups", params={"service_id": service_id})).json()

    assert [s["name"] for s in services] == ["Netflix"]
    assert len(groups) == 1 and groups[0]["slots_free"] == 3
    assert "secret_owner" not in str(groups) and "owner_id" not in groups[0]
