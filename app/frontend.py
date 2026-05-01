from urllib.parse import urlencode

from fastapi.responses import RedirectResponse

from .config import settings


def _frontend_base() -> str:
    return str(settings.FRONTEND_BASE_URL).rstrip("/")


def frontend_url(path: str = "/", query: dict | None = None) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    url = f"{_frontend_base()}{normalized}"
    if query:
        pairs = {k: v for k, v in query.items() if v is not None and v != ""}
        if pairs:
            url = f"{url}?{urlencode(pairs)}"
    return url


def admin_url(route: str = "/dashboard", query: dict | None = None) -> str:
    normalized = route if route.startswith("/") else f"/{route}"
    url = f"{_frontend_base()}/admin/#{normalized}"
    if query:
        pairs = {k: v for k, v in query.items() if v is not None and v != ""}
        if pairs:
            url = f"{url}?{urlencode(pairs)}"
    return url


def redirect_frontend(path: str = "/", query: dict | None = None, status_code: int = 302) -> RedirectResponse:
    return RedirectResponse(url=frontend_url(path, query), status_code=status_code)


def redirect_admin(route: str = "/dashboard", query: dict | None = None, status_code: int = 302) -> RedirectResponse:
    return RedirectResponse(url=admin_url(route, query), status_code=status_code)
