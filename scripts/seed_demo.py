"""Демо-данные для просмотра сайта: пара организаторов и несколько групп.
Запуск: python -m scripts.seed_demo"""

import asyncio

from shared.db.session import engine, session_maker
from shared.services import groups as groups_svc
from shared.services.catalog import seed_services
from shared.services.users import register_user


async def main() -> None:
    await seed_services(session_maker)
    services = await groups_svc.list_active_services(session_maker)
    for i, service in enumerate(services[:6]):
        owner = await register_user(session_maker, 9000 + i, f"demo_owner{i}")
        total = min(service.max_members, 4)
        await groups_svc.create_group(session_maker, owner.id, service.id, total, total - 1, 5 + i)
    await engine.dispose()
    print("Демо-группы созданы")


if __name__ == "__main__":
    asyncio.run(main())
