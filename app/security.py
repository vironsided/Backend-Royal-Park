from passlib.context import CryptContext
from itsdangerous import URLSafeSerializer
from starlette.requests import Request
from starlette.responses import Response
from .config import settings

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
serializer = URLSafeSerializer(settings.SESSION_SECRET_KEY, salt="session-salt")


def _use_cross_site_cookie() -> bool:
    frontend = (settings.FRONTEND_BASE_URL or "").strip().lower()
    if not frontend.startswith("https://"):
        return False
    return "localhost" not in frontend and "127.0.0.1" not in frontend


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def set_session(response: Response, user_id: int):
    token = serializer.dumps({"user_id": user_id})
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
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return None
    try:
        data = serializer.loads(token)
        return int(data.get("user_id"))
    except Exception:
        return None
