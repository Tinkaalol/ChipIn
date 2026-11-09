import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from shared.db.models import TelegramLinkToken, User
from shared.services.groups import GroupError, SessionMaker

TOKEN_TTL = timedelta(minutes=10)
START_PREFIX = "link_"


class LinkError(GroupError):
    """Привязка не удалась; текст понятен пользователю."""


@dataclass
class LinkResult:
    user: User


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _as_utc(moment: datetime) -> datetime:
    # SQLite отдаёт время без пояса, хотя мы всегда пишем UTC
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def create_link_token(session_maker: SessionMaker, user_id: int) -> str:
    """Создаёт одноразовый токен, в базе остаётся только хэш. Старые неиспользованные гаснут."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        await session.execute(
            delete(TelegramLinkToken).where(
                TelegramLinkToken.user_id == user_id,
                (TelegramLinkToken.used_at.is_(None)) | (TelegramLinkToken.expires_at < now),
            )
        )
        session.add(
            TelegramLinkToken(
                user_id=user_id, token_hash=hash_token(token), expires_at=now + TOKEN_TTL
            )
        )
    return token


async def link_telegram(
    session_maker: SessionMaker, token: str, telegram_id: int, username: str | None
) -> LinkResult:
    """Привязывает telegram_id к пользователю, которому выдан токен."""
    now = datetime.now(UTC)
    async with session_maker() as session:
        row = await session.scalar(
            select(TelegramLinkToken).where(TelegramLinkToken.token_hash == hash_token(token))
        )
        if row is None:
            raise LinkError("Ссылка недействительна. Создайте новую в настройках на сайте.")
        if row.used_at is not None:
            raise LinkError("Эта ссылка уже использована. Создайте новую в настройках на сайте.")
        if _as_utc(row.expires_at) <= now:
            raise LinkError("Срок ссылки истёк (10 минут). Создайте новую в настройках на сайте.")
        user = await session.get(User, row.user_id)
        if user is None or user.is_banned:
            raise LinkError("Этот аккаунт недоступен.")
        owner = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if owner is not None and owner.id != user.id:
            raise LinkError(
                "Этот Telegram уже привязан к другому аккаунту. "
                "Отвяжите его там в настройках сайта или войдите в тот аккаунт."
            )
        # used_at ставится условным UPDATE: два одновременных /start не пройдут оба
        claimed = await session.execute(
            update(TelegramLinkToken)
            .where(TelegramLinkToken.id == row.id, TelegramLinkToken.used_at.is_(None))
            .values(used_at=now)
        )
        if claimed.rowcount == 0:
            raise LinkError("Эта ссылка уже использована. Создайте новую в настройках на сайте.")
        user.telegram_id = telegram_id
        user.telegram_username = username
        user.username = username
        user.telegram_linked_at = now
        try:
            await session.commit()
        except IntegrityError:
            # Тот же telegram_id успели привязать параллельно
            await session.rollback()
            raise LinkError("Этот Telegram уже привязан к другому аккаунту.") from None
        return LinkResult(user)


async def unlink_telegram(session_maker: SessionMaker, user_id: int) -> None:
    async with session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return
        user.telegram_id = None
        user.telegram_username = None
        user.telegram_linked_at = None
        await session.commit()


async def set_notifications(session_maker: SessionMaker, user_id: int, enabled: bool) -> None:
    async with session_maker() as session:
        user = await session.get(User, user_id)
        if user is not None:
            user.notify_enabled = enabled
            await session.commit()
