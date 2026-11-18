import httpx
import pytest

from db import SchemaError, check_schema

GOOD_COLUMNS = {
    c: {"type": "string"}
    for c in (
        "id service_slug service_name creator_tg_username creator_email price_per_slot max_slots "
        "total_slots current_slots occupied_slots billing_date invite_link members status "
        "created_at"
    ).split()
}


def transport(definitions: dict | None, status: int = 200) -> httpx.MockTransport:
    body = {"definitions": definitions} if definitions is not None else {}
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


def groups(required: list[str], drop: tuple[str, ...] = ()) -> dict:
    props = {k: dict(v) for k, v in GOOD_COLUMNS.items() if k not in drop}
    props["id"]["default"] = "gen_random_uuid()"
    props["total_slots"]["default"] = 4  # NOT NULL, но с default: бот заполнять не обязан
    return {"groups": {"properties": props, "required": required}}


async def run(definitions, status: int = 200) -> None:
    await check_schema("https://x.supabase.co", "key", transport(definitions, status))


async def test_suitable_table_passes() -> None:
    await run(groups(["id", "service_slug", "creator_tg_username", "max_slots"]))


async def test_missing_columns_are_named() -> None:
    with pytest.raises(SchemaError, match="price_per_slot"):
        await run(groups(["id"], drop=("price_per_slot",)))


async def test_not_null_column_the_bot_does_not_fill() -> None:
    definitions = groups(["id", "service_slug", "owner_phone"])
    definitions["groups"]["properties"]["owner_phone"] = {"type": "string"}
    with pytest.raises(SchemaError, match="owner_phone"):
        await run(definitions)


async def test_not_null_with_default_is_fine() -> None:
    await run(groups(["id", "total_slots", "creator_email"]))


async def test_no_table_or_bad_key() -> None:
    with pytest.raises(SchemaError, match="нет таблицы"):
        await run({})
    with pytest.raises(SchemaError, match="401"):
        await run(None, status=401)
