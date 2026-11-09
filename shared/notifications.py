"""Очередь уведомлений (outbox): события пишутся в транзакции бизнес-операции,
а отправляет их бот отдельным воркером. Здесь же все тексты уведомлений.

В сообщениях нет паролей и данных карт: это правило проекта, а не только форматирования.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from html import escape
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.dates import ALMATY, next_payment_date
from shared.db.models import NotificationOutbox, NotificationStatus, User
from shared.formatting import contact_html, money


class NotificationType(StrEnum):
    NEW_JOIN_REQUEST = "new_join_request"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    MEMBER_LEFT = "member_left"
    MEMBER_REMOVED = "member_removed"
    SLOT_FREED = "slot_freed"
    PAYMENT_REMINDER = "payment_reminder"
    ACCOUNT_BLOCKED = "account_blocked"
    # Дополнительно к основному списку: раньше эти сообщения тоже слались напрямую
    ACCOUNT_UNBLOCKED = "account_unblocked"
    GROUP_CLOSED = "group_closed"


# callback_data существующих кнопок бота (обработчики в bot/handlers); совпадение проверяет тест
DECIDE_OK = "m:ok:"
DECIDE_NO = "m:no:"
REPORT_NEW = "r:new:"


@dataclass(frozen=True)
class Button:
    text: str
    url: str | None = None
    callback: str | None = None


@dataclass
class Rendered:
    text: str  # HTML для parse_mode=HTML
    buttons: list[list[Button]] = field(default_factory=list)


def user_ref(user: User, role: str) -> dict[str, Any]:
    """Как показать пользователя в тексте: username или ссылку на профиль."""
    return {f"{role}_username": user.username, f"{role}_telegram_id": user.telegram_id}


def _contact(payload: Mapping[str, Any], role: str) -> str:
    return contact_html(payload.get(f"{role}_telegram_id"), payload.get(f"{role}_username"))


async def enqueue(
    session: AsyncSession, user_id: int, type: NotificationType, payload: Mapping[str, Any]
) -> NotificationOutbox:
    """Кладёт уведомление в очередь в текущей транзакции: при откате его не останется.

    Ничего не коммитит. Если слать некуда (нет telegram_id) или получатель выключил
    уведомления, событие сразу получает статус skipped.
    """
    user = await session.get(User, user_id)
    status, error = NotificationStatus.PENDING, None
    if user is None or user.telegram_id is None:
        status, error = NotificationStatus.SKIPPED, "Telegram не привязан"
    elif not user.notify_enabled:
        status, error = NotificationStatus.SKIPPED, "Уведомления выключены"
    row = NotificationOutbox(
        user_id=user_id, type=str(type), payload=dict(payload), status=status, error=error
    )
    session.add(row)
    return row


# --- Тексты ---


def _title(p: Mapping[str, Any]) -> str:
    return escape(p["title"])


def _site(site_url: str, path: str, text: str) -> list[Button]:
    # Telegram принимает в кнопках только корректные адреса, поэтому без https кнопку не рисуем
    if not site_url.startswith("https://"):
        return []
    return [Button(text, url=f"{site_url.rstrip('/')}{path}")]


def _group_link(site_url: str, p: Mapping[str, Any], text: str) -> list[list[Button]]:
    row = _site(site_url, f"/groups/{p['group_id']}", text)
    return [row] if row else []


def _find_link(site_url: str) -> list[list[Button]]:
    row = _site(site_url, "/groups", "Найти другую группу")
    return [row] if row else []


def _new_join_request(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    text = (
        f"Новая заявка в группу {_title(p)}: {_contact(p, 'applicant')}\n"
        f"Свободных мест: {p['slots_free']}"
    )
    decide = [
        Button("Принять", callback=f"{DECIDE_OK}{p['membership_id']}"),
        Button("Отклонить", callback=f"{DECIDE_NO}{p['membership_id']}"),
    ]
    return Rendered(text, [decide, *_group_link(site_url, p, "Открыть на сайте")])


def _request_approved(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    next_date = next_payment_date(p["billing_day"], today)
    text = (
        f"Ваша заявка в группу {_title(p)} принята.\n"
        f"Организатор: {_contact(p, 'owner')}\n"
        f"Ближайшая оплата: {next_date:%d.%m.%Y} ({p['billing_day']}-го числа каждого месяца), "
        f"взнос {money(p['price'], p['currency'])}.\n"
        "Деньги переводите организатору напрямую, бот их не принимает. "
        "Пароли и данные карт в чат не пересылайте."
    )
    return Rendered(text, _group_link(site_url, p, "Открыть группу"))


def _request_rejected(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    text = (
        f"К сожалению, организатор не смог принять вашу заявку в группу {_title(p)}. "
        "Спасибо за интерес! Новую заявку в эту группу можно подать через 24 часа, "
        "а пока загляните в другие группы."
    )
    return Rendered(text, _find_link(site_url))


def _member_left(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    who = _contact(p, "member")
    if p["was_approved"]:
        text = f"Участник {who} вышел из группы {_title(p)}. Место снова свободно."
    else:
        text = f"{who} отозвал заявку в группу {_title(p)}."
    return Rendered(text, _group_link(site_url, p, "Открыть группу"))


def _member_removed(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    if p.get("reason"):
        text = f"Причина удаления из группы {_title(p)}: {escape(p['reason'])}"
    else:
        text = f"Организатор удалил вас из группы {_title(p)}."
    report = Button(
        "Пожаловаться на организатора", callback=f"{REPORT_NEW}{p['group_id']}:{p['owner_id']}"
    )
    return Rendered(text, [[report], *_find_link(site_url)])


def _slot_freed(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    text = (
        f"В группе {_title(p)} освободилось место: участник {_contact(p, 'member')} "
        "заблокирован и удалён из группы. Набор снова открыт."
    )
    return Rendered(text, _group_link(site_url, p, "Открыть группу"))


def _payment_reminder(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    when = "Сегодня" if p["when"] == "today" else "Завтра"
    head = f"{when} оплата подписки {_title(p)}.\n"
    price = money(p["price"], p["currency"])
    if p["role"] == "owner":
        text = head + f"Участники (их {p['members']}) переведут вам по {price}."
    else:
        text = head + f"Ваш взнос: {price}.\nПереведите его организатору: {_contact(p, 'owner')}."
    return Rendered(text, _group_link(site_url, p, "Открыть группу"))


def _account_blocked(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    return Rendered("Ваш доступ к сервису ограничен администратором.")


def _account_unblocked(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    return Rendered("Ваш доступ к сервису восстановлен.", _find_link(site_url))


def _group_closed(p: Mapping[str, Any], site_url: str, today: date) -> Rendered:
    if p.get("by_ban"):
        text = f"Группа {_title(p)} закрыта: организатор заблокирован."
    else:
        text = f"Организатор закрыл группу {_title(p)}."
    return Rendered(text, _find_link(site_url))


_TEMPLATES: dict[NotificationType, Callable[[Mapping[str, Any], str, date], Rendered]] = {
    NotificationType.NEW_JOIN_REQUEST: _new_join_request,
    NotificationType.REQUEST_APPROVED: _request_approved,
    NotificationType.REQUEST_REJECTED: _request_rejected,
    NotificationType.MEMBER_LEFT: _member_left,
    NotificationType.MEMBER_REMOVED: _member_removed,
    NotificationType.SLOT_FREED: _slot_freed,
    NotificationType.PAYMENT_REMINDER: _payment_reminder,
    NotificationType.ACCOUNT_BLOCKED: _account_blocked,
    NotificationType.ACCOUNT_UNBLOCKED: _account_unblocked,
    NotificationType.GROUP_CLOSED: _group_closed,
}


def render(
    type: str, payload: Mapping[str, Any], site_url: str = "", today: date | None = None
) -> Rendered:
    """Собирает текст и кнопки уведомления. Ссылки на сайт добавляются, если задан https-адрес."""
    today = today or datetime.now(ALMATY).date()
    return _TEMPLATES[NotificationType(type)](payload, site_url, today)
