import hmac
import secrets

from fastapi import HTTPException, Request

from shared.db.models import User


class LoginRequired(Exception):
    """Страница требует входа: обработчик в main.py отправляет на /login."""


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


async def check_csrf(request: Request) -> None:
    """Для всех POST: токен из сессии должен совпасть с полем формы или заголовком HTMX."""
    expected = request.session.get("csrf", "")
    form = await request.form()
    sent = str(form.get("csrf_token") or request.headers.get("x-csrf-token") or "")
    if not expected or not hmac.compare_digest(expected.encode(), sent.encode()):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен.")


async def optional_user(request: Request) -> User | None:
    user_id = request.session.get("uid")
    if not user_id:
        return None
    async with request.app.state.session_maker() as session:
        user = await session.get(User, user_id)
    if user is None or user.is_banned:
        request.session.clear()
        return None
    return user


async def require_user(request: Request) -> User:
    user = await optional_user(request)
    if user is None:
        raise LoginRequired
    return user
