"""telegram linking

Revision ID: b7d2f1a9c3e4
Revises: 10195e7cf2a3
Create Date: 2026-11-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2f1a9c3e4"
down_revision: str | Sequence[str] | None = "10195e7cf2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Привязка Telegram: telegram_id становится необязательным, добавлены поля и токены."""
    with op.batch_alter_table("users") as batch:
        batch.alter_column("telegram_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("telegram_username", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("telegram_linked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("notify_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    # Все прежние пользователи пришли из Telegram, значит уже привязаны
    op.execute(
        "UPDATE users SET telegram_username = username, telegram_linked_at = created_at "
        "WHERE telegram_id IS NOT NULL"
    )
    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_telegram_link_tokens_user_id", "telegram_link_tokens", ["user_id"])


def downgrade() -> None:
    """Откат возможен, только если у всех пользователей есть telegram_id."""
    op.drop_index("ix_telegram_link_tokens_user_id", table_name="telegram_link_tokens")
    op.drop_table("telegram_link_tokens")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("notify_enabled")
        batch.drop_column("telegram_linked_at")
        batch.drop_column("telegram_username")
        batch.alter_column("telegram_id", existing_type=sa.BigInteger(), nullable=False)
