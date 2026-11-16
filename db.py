from typing import Any
from uuid import UUID

from supabase import AsyncClient, acreate_client

Row = dict[str, Any]

STATUS_COLLECTING = "collecting"
STATUS_FULL = "full"


async def connect(url: str, key: str) -> "Database":
    return Database(await acreate_client(url, key))


class Database:
    """Доступ к таблицам Supabase: available_services и groups."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def list_services(self) -> list[Row]:
        response = (
            await self.client.table("available_services").select("*").order("title").execute()
        )
        return list(response.data)

    async def get_service(self, slug: str) -> Row | None:
        response = (
            await self.client.table("available_services").select("*").eq("slug", slug).execute()
        )
        return response.data[0] if response.data else None

    async def get_group(self, group_id: str) -> Row | None:
        try:
            UUID(group_id)
        except ValueError:
            return None  # из ссылки может прийти что угодно, а Supabase на не-uuid отвечает ошибкой
        response = await self.client.table("groups").select("*").eq("id", group_id).execute()
        return response.data[0] if response.data else None

    async def list_open_groups(self, service_slug: str | None = None) -> list[Row]:
        """Группы, где идёт набор и есть свободные места (сначала те, где мест больше)."""
        query = self.client.table("groups").select("*").eq("status", STATUS_COLLECTING)
        if service_slug is not None:
            query = query.eq("service_slug", service_slug)
        response = await query.execute()
        # PostgREST не сравнивает два столбца между собой, поэтому места считаем здесь
        groups = [g for g in response.data if free_slots(g) > 0]
        return sorted(groups, key=free_slots, reverse=True)


def is_joinable(group: Row) -> bool:
    return group["status"] == STATUS_COLLECTING and free_slots(group) > 0


def free_slots(group: Row) -> int:
    return int(group["max_slots"]) - int(group["current_slots"])
