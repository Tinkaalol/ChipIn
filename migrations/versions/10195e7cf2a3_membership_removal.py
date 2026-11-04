"""membership removal

Revision ID: 10195e7cf2a3
Revises: 64ecbb14f655
Create Date: 2026-11-04 13:45:56.059597

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "10195e7cf2a3"
down_revision: str | Sequence[str] | None = "64ecbb14f655"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Участника может удалить организатор: статус removed и кто/когда/почему удалил.

    Новое значение статуса отдельной правки схемы не требует: enum хранится строкой
    без CHECK-ограничения.
    """
    with op.batch_alter_table("memberships") as batch:
        batch.add_column(sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("removed_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("removal_reason", sa.Text(), nullable=True))
        batch.create_foreign_key("fk_memberships_removed_by_users", "users", ["removed_by"], ["id"])


def downgrade() -> None:
    """Удалённые участники при откате становятся вышедшими (left)."""
    op.execute("UPDATE memberships SET status = 'left' WHERE status = 'removed'")
    with op.batch_alter_table("memberships") as batch:
        batch.drop_constraint("fk_memberships_removed_by_users", type_="foreignkey")
        batch.drop_column("removal_reason")
        batch.drop_column("removed_by")
        batch.drop_column("removed_at")
