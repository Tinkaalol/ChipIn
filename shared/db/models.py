import enum
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_CURRENCY = "KZT"  # все подписки считаются в тенге


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class GroupStatus(enum.StrEnum):
    OPEN = "open"
    FULL = "full"
    CLOSED = "closed"


class MembershipStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    LEFT = "left"
    REMOVED = "removed"  # удалён организатором


class NotificationStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # у получателя нет Telegram или уведомления выключены


class ReportStatus(enum.StrEnum):
    NEW = "new"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


def _enum(cls: type[enum.Enum]) -> Enum:
    # Храним значения (open), а не имена; без нативного ENUM — одинаково на SQLite и PostgreSQL
    return Enum(
        cls,
        values_callable=lambda e: [m.value for m in e],
        native_enum=False,
        length=16,
        validate_strings=True,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # None у тех, кто пришёл на сайт без Telegram или отвязал его; уникален среди привязанных
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    telegram_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    language: Mapped[str] = mapped_column(String(8), default="ru")
    is_banned: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (Index("uq_services_name_plan", "name", "plan_name", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    plan_name: Mapped[str] = mapped_column(String(64))
    price_month: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    max_members: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    price_per_member: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    slots_total: Mapped[int]
    slots_free: Mapped[int]
    billing_day: Mapped[int]
    status: Mapped[GroupStatus] = mapped_column(_enum(GroupStatus), default=GroupStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # День, когда по группе уже отправлено напоминание об оплате (защита от дублей)
    last_reminded_on: Mapped[date | None] = mapped_column(Date)


class Membership(Base):
    __tablename__ = "memberships"
    # Один человек не может иметь две живые заявки/участия в группе (защита от двойного клика)
    __table_args__ = (
        Index(
            "uq_memberships_active",
            "group_id",
            "user_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'approved')"),
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[MembershipStatus] = mapped_column(
        _enum(MembershipStatus), default=MembershipStatus.PENDING
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда организатор принял или отклонил заявку (от этого считается пауза перед повтором)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Удаление участника организатором
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    removal_reason: Mapped[str | None] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(_enum(ReportStatus), default=ReportStatus.NEW)


class TelegramLinkToken(Base):
    """Одноразовая ссылка привязки Telegram. Сам токен не хранится, только его SHA-256."""

    __tablename__ = "telegram_link_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationOutbox(Base):
    """Очередь уведомлений: пишется в той же транзакции, что и бизнес-операция."""

    __tablename__ = "notification_outbox"
    __table_args__ = (Index("ix_notification_outbox_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus), default=NotificationStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
