from typing import Any
from uuid import UUID

from supabase import AsyncClient, acreate_client

Row = dict[str, Any]

STATUS_COLLECTING = "collecting"
STATUS_FULL = "full"

APP_PENDING = "pending"
APP_APPROVED = "approved"
APP_REJECTED = "rejected"
APP_FAILED = "failed"

JOIN_RETRIES = 5


class GroupUnavailable(Exception):
    """В группе нет мест или набор закрыт."""


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

    async def create_group(
        self, service_slug: str, creator_username: str, creator_email: str, max_slots: int
    ) -> Row:
        """Новая группа: создатель уже занял одно место."""
        values = {
            "service_slug": service_slug,
            "creator_tg_username": creator_username,
            "creator_email": creator_email,
            "max_slots": max_slots,
            "current_slots": 1,
            "status": STATUS_FULL if max_slots <= 1 else STATUS_COLLECTING,
        }
        response = await self.client.table("groups").insert(values).execute()
        return response.data[0]

    async def join_group(self, group_id: str) -> Row:
        """Занимает место: current_slots + 1, при заполнении status = full.

        Обновление условное (current_slots должен остаться прежним), поэтому два
        одновременных подтверждения не займут одно и то же место.
        """
        for _ in range(JOIN_RETRIES):
            group = await self.get_group(group_id)
            if group is None or not is_joinable(group):
                raise GroupUnavailable
            taken = int(group["current_slots"]) + 1
            values: Row = {"current_slots": taken}
            if taken >= int(group["max_slots"]):
                values["status"] = STATUS_FULL
            response = await (
                self.client.table("groups")
                .update(values)
                .eq("id", group_id)
                .eq("current_slots", group["current_slots"])
                .eq("status", STATUS_COLLECTING)
                .execute()
            )
            if response.data:
                return response.data[0]
        raise RuntimeError("Не удалось занять место: группу меняют слишком часто")

    async def create_application(self, values: Row) -> Row:
        response = await self.client.table("applications").insert(values).execute()
        return response.data[0]

    async def claim_application(self, app_id: str, status: str) -> Row | None:
        """pending -> status. None, если заявку уже обработали (или её нет)."""
        try:
            UUID(app_id)
        except ValueError:
            return None
        response = await (
            self.client.table("applications")
            .update({"status": status})
            .eq("id", app_id)
            .eq("status", APP_PENDING)
            .execute()
        )
        return response.data[0] if response.data else None

    async def set_application_status(self, app_id: str, status: str) -> None:
        await (
            self.client.table("applications").update({"status": status}).eq("id", app_id).execute()
        )

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
