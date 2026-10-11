from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.admin import auth
from app.config import get_settings

router = APIRouter(include_in_schema=False)
STATIC_DIR = Path(__file__).parent / "static"


def _authentication_enforced() -> bool:
    return get_settings().app_env == "production"


def _session_valid(request: Request) -> bool:
    settings = get_settings()
    return auth.session_token_is_valid(
        request.cookies.get(auth.SESSION_COOKIE, ""), settings.admin_api_key or ""
    )


def _login_redirect(error: str, next_path: str | None = None) -> RedirectResponse:
    target = f"{auth.LOGIN_PATH}?error={quote(error)}"
    if next_path and next_path != "/admin":
        target += f"&next={quote(next_path, safe='/')}"
    return RedirectResponse(target, status_code=303)


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/admin")


@router.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/admin/login")
async def admin_login_page(request: Request, next: str = "/admin") -> Response:
    if not _authentication_enforced():
        # 非生产环境不启用鉴权，登录页没有意义。
        return RedirectResponse("/admin", status_code=303)
    if _session_valid(request):
        return RedirectResponse(auth.safe_next_path(next), status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@router.post("/admin/login")
async def admin_login_submit(request: Request) -> Response:
    if not _authentication_enforced():
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    next_path = auth.safe_next_path(str(form.get("next", "/admin")))
    if auth.is_cross_site(request):
        return _login_redirect("origin")
    configured = get_settings().admin_api_key or ""
    if not configured:
        return _login_redirect("unconfigured")
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not auth.credentials_match(username, password, configured):
        return _login_redirect("credentials", next_path)

    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session_token(configured),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/admin/logout")
async def admin_logout() -> Response:
    response = RedirectResponse(auth.LOGIN_PATH, status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
