import os
import re
from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response
from .config import settings

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# audit F-06: the signed session token now carries a timestamp and is verified
# with max_age, so a stolen/leaked token is not valid forever (matches cookie TTL).
SESSION_MAX_AGE_SECONDS = 60 * 60 * 8  # 8 hours
serializer = URLSafeTimedSerializer(settings.SESSION_SECRET_KEY, salt="session-salt")


def validate_password_strength(password: str) -> None:
    """audit F-12: единая парольная политика. Бросает ValueError при нарушении."""
    pw = password or ""
    if len(pw) < 10:
        raise ValueError("Пароль должен быть не менее 10 символов")
    if not re.search(r"[A-Za-zА-Яа-яƏəÖöÜüÇçĞğŞşİı]", pw) or not re.search(r"\d", pw):
        raise ValueError("Пароль должен содержать и буквы, и цифры")


def _use_cross_site_cookie() -> bool:
    # audit F-11: with the same-origin proxy the cookie is first-party, so it
    # should be SameSite=Lax. Allow an explicit override for deploys.
    override = os.getenv("COOKIE_SAMESITE", "").strip().lower()
    if override in {"lax", "strict"}:
        return False
    if override == "none":
        return True
    frontend = (settings.FRONTEND_BASE_URL or "").strip().lower()
    if not frontend.startswith("https://"):
        return False
    return "localhost" not in frontend and "127.0.0.1" not in frontend


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def make_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": int(user_id)})


def _read_bearer_token(request: Request) -> str | None:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def set_session(response: Response, user_id: int):
    token = make_session_token(user_id)
    cross_site = _use_cross_site_cookie()
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="none" if cross_site else "lax",
        secure=cross_site,
        max_age=60 * 60 * 8,  # 8 часов
        path="/",
    )


def clear_session(response: Response):
    cross_site = _use_cross_site_cookie()
    response.delete_cookie(
        settings.COOKIE_NAME,
        path="/",
        samesite="none" if cross_site else "lax",
        secure=cross_site,
    )


def get_user_id_from_session(request: Request) -> int | None:
    token = request.cookies.get(settings.COOKIE_NAME) or _read_bearer_token(request)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return int(data.get("user_id"))
    except Exception:
        return None
