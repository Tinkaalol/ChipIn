"""prices in tenge

Revision ID: a1c2e3f4b5d6
Revises: 68169be9f198
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "a1c2e3f4b5d6"
down_revision: str | Sequence[str] | None = "68169be9f198"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Каталог по умолчанию: (название, тариф) -> (цена в рублях, цена в тенге). Цены примерные.
SEED_PRICES = {
    ("Spotify", "Family"): (Decimal("299"), Decimal("2800")),
    ("YouTube Premium", "Family"): (Decimal("399"), Decimal("3500")),
    ("ChatGPT", "Plus"): (Decimal("2000"), Decimal("10000")),
    ("Яндекс Плюс", "Мульти"): (Decimal("399"), Decimal("2000")),
}

services = sa.table(
    "services",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("plan_name", sa.String),
    sa.column("price_month", sa.Numeric(10, 2)),
    sa.column("currency", sa.String),
)
groups = sa.table(
    "groups",
    sa.column("id", sa.Integer),
    sa.column("service_id", sa.Integer),
    sa.column("slots_total", sa.Integer),
    sa.column("price_per_member", sa.Numeric(10, 2)),
)


def _convert(currency: str, from_index: int, to_index: int) -> None:
    """Меняет валюту и цены стандартных сервисов, затем пересчитывает взносы в группах."""
    bind = op.get_bind()
    for (name, plan), prices in SEED_PRICES.items():
        bind.execute(
            sa.update(services)
            .where(
                services.c.name == name,
                services.c.plan_name == plan,
                services.c.price_month == prices[from_index],
            )
            .values(price_month=prices[to_index])
        )
    bind.execute(sa.update(services).values(currency=currency))
    rows = bind.execute(
        sa.select(groups.c.id, services.c.price_month, groups.c.slots_total).join(
            services, groups.c.service_id == services.c.id
        )
    ).all()
    for group_id, price, slots_total in rows:
        per_member = (Decimal(price) / slots_total).quantize(Decimal("0.01"), ROUND_CEILING)
        bind.execute(
            sa.update(groups).where(groups.c.id == group_id).values(price_per_member=per_member)
        )


def upgrade() -> None:
    _convert("KZT", from_index=0, to_index=1)


def downgrade() -> None:
    _convert("RUB", from_index=1, to_index=0)
