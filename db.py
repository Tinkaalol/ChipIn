import json
import logging
from typing import Any
from uuid import UUID

import httpx
from supabase import AsyncClient, acreate_client

logger = logging.getLogger(__name__)

Row = dict[str, Any]

STATUS_OPEN = "open"
STATUS_FULL = "full"
MEMBER_ACTIVE = "active"
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"

JOIN_RETRIES = 5

# Все колонки, которые бот заполняет при создании группы. Размер группы дублируется в
# max_slots и total_slots, занятые места в current_slots и occupied_slots: сайт может читать любую.
INSERT_COLUMNS = {
    "service_slug",
    "service_name",
    "creator_tg_username",
    "creator_email",
    "price_per_slot",
    "max_slots",
    "total_slots",
    "current_slots",
    "occupied_slots",
    "billing_date",
    "invite_link",
    "members",
    "status",
}
GROUP_COLUMNS = INSERT_COLUMNS
# Организатор email не вводит, а колонка creator_email обязательная
CREATOR_EMAIL_PLACEHOLDER = "not_required"


class GroupUnavailable(Exception):
    """Группа не найдена, закрыта или в ней нет мест."""


class AlreadyMember(Exception):
    """Этот пользователь уже в группе."""


class SchemaError(Exception):
    """Таблица groups в Supabase не подходит боту; в тексте сказано, что исправить."""


async def check_schema(
    url: str, key: str, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Сверяет таблицу groups с тем, что нужно боту. Читает описание API (OpenAPI) Supabase."""
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=transport, timeout=20) as http:
        response = await http.get(f"{url}/rest/v1/", headers=headers)
    if response.status_code != 200:
        raise SchemaError(f"Supabase ответил {response.status_code}: проверьте URL и ключ.")
    table = response.json().get("definitions", {}).get("groups")
    if table is None:
        raise SchemaError("В Supabase нет таблицы groups.")
    columns = set(table.get("properties", {}))
    missing = GROUP_COLUMNS - columns
    if missing:
        raise SchemaError(
            f"В groups нет колонок: {', '.join(sorted(missing))}. Выполните supabase_schema.sql."
        )
    # required в OpenAPI: NOT NULL. Бот обязан сам заполнить те из них, у которых нет default
    properties = table.get("properties", {})
    no_default = {c for c in table.get("required", []) if "default" not in properties.get(c, {})}
    blocked = no_default - INSERT_COLUMNS
    if blocked:
        raise SchemaError(
            f"Колонки {', '.join(sorted(blocked))} обязательны, а бот их не заполняет. "
            "Выполните supabase_schema.sql."
        )


async def connect(url: str, key: str) -> "Database":
    return Database(await acreate_client(url, key))


def total_slots(group: Row) -> int:
    return int(group.get("max_slots") or group.get("total_slots") or 0)


def occupied_slots(group: Row) -> int:
    current = group.get("current_slots")
    return int(current if current is not None else group.get("occupied_slots") or 0)


def free_slots(group: Row) -> int:
    return total_slots(group) - occupied_slots(group)


def is_joinable(group: Row) -> bool:
    return group["status"] == STATUS_OPEN and free_slots(group) > 0


def members_of(group: Row) -> list[Row]:
    return list(group.get("members") or [])


def has_member(group: Row, tg_id: int) -> bool:
    return any(m.get("tg_id") == tg_id for m in members_of(group))


def is_owner(group: Row, tg_id: int, username: str | None) -> bool:
    if any(m.get("tg_id") == tg_id and m.get("role") == ROLE_OWNER for m in members_of(group)):
        return True
    creator = (group.get("creator_tg_username") or "").lower()
    return bool(username) and creator == f"@{username}".lower()


class Database:
    """Таблица groups в Supabase. Справочник сервисов лежит в catalog.py, не в базе."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def get_group(self, group_id: str) -> Row | None:
        try:
            UUID(group_id)
        except ValueError:
            return None  # из ссылки может прийти что угодно, а Supabase на не-uuid отвечает ошибкой
        response = await self.client.table("groups").select("*").eq("id", group_id).execute()
        return response.data[0] if response.data else None

    async def list_open_groups(self, service_slug: str | None = None) -> list[Row]:
        """Группы с набором и свободными местами; сначала те, где мест больше."""
        query = self.client.table("groups").select("*").eq("status", STATUS_OPEN)
        if service_slug is not None:
            query = query.eq("service_slug", service_slug)
        response = await query.execute()
        # PostgREST не сравнивает два столбца между собой, поэтому места считаем здесь
        groups = [g for g in response.data if free_slots(g) > 0]
        return sorted(groups, key=free_slots, reverse=True)

    async def create_group(self, values: Row) -> Row:
        response = await self.client.table("groups").insert(values).execute()
        return response.data[0]

    async def join_group(self, group_id: str, member: Row) -> Row:
        """Добавляет участника, занимает место, при заполнении ставит status = full.

        Обновление условное (current_slots должен остаться прежним), поэтому два
        одновременных вступления не займут одно и то же место.
        """
        for _ in range(JOIN_RETRIES):
            group = await self.get_group(group_id)
            if group is None or not is_joinable(group):
                raise GroupUnavailable
            if has_member(group, member["tg_id"]):
                raise AlreadyMember
            taken = occupied_slots(group) + 1
            values: Row = {
                "members": [*members_of(group), member],
                "current_slots": taken,
                "occupied_slots": taken,
            }
            if taken >= total_slots(group):
                values["status"] = STATUS_FULL
            response = await (
                self.client.table("groups")
                .update(values)
                .eq("id", group_id)
                .eq("current_slots", group["current_slots"])  # условие против гонки
                .eq("status", STATUS_OPEN)
                .execute()
            )
            if response.data:
                return response.data[0]
        raise RuntimeError("Не удалось занять место: группу меняют слишком часто")

    async def list_user_groups(self, tg_id: int, username: str | None) -> list[Row]:
        """Группы, где человек организатор или участник (по tg_id в members или по @username)."""
        table = self.client.table
        by_member = await (
            table("groups")
            .select("*")
            .filter("members", "cs", json.dumps([{"tg_id": tg_id}]))
            .execute()
        )
        found = {g["id"]: g for g in by_member.data}
        if username:  # старые группы без записи организатора в members
            by_creator = await (
                table("groups").select("*").eq("creator_tg_username", f"@{username}").execute()
            )
            found |= {g["id"]: g for g in by_creator.data}
        return list(found.values())
