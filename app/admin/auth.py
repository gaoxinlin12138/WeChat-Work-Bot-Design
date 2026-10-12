"""管理台鉴权：会话 Cookie、来源校验与受保护路径判定。

浏览器走登录页拿到签名 Cookie；自动化脚本继续用 `X-Admin-Key`；旧版 Basic
仍然接受，兼容既有工具，但不再回 `WWW-Authenticate`，否则浏览器会弹出
原生登录框而不是管理台自己的登录页。

Cookie 用 HMAC 无状态签名，密钥直接派生自 `ADMIN_API_KEY`，因此不引入新配置、
不新增依赖，也不需要服务端会话表。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from urllib.parse import urlparse

from fastapi import Request

SESSION_COOKIE = "wecom_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
ADMIN_USERNAME = "admin"

LOGIN_PATH = "/admin/login"
LOGOUT_PATH = "/admin/logout"
ADMIN_PAGE_PATHS = frozenset({"/admin", "/admin/"})
# 登录/退出自身不能要求已登录，否则无法进入。
PUBLIC_ADMIN_PATHS = frozenset({LOGIN_PATH, LOGOUT_PATH})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# 与旧实现保持一致：/admin-static 不在此列表内，静态资源保持可匿名加载。
PROTECTED_PREFIXES = ("/admin/", "/knowledge", "/business", "/docs", "/redoc", "/openapi.json")


def is_protected_path(path: str) -> bool:
    return path == "/admin" or path.startswith(PROTECTED_PREFIXES)


def prefers_html(request: Request) -> bool:
    """区分浏览器导航与前端 fetch、脚本调用。"""
    return "text/html" in request.headers.get("accept", "")


def is_browser_navigation(request: Request) -> bool:
    return prefers_html(request) or request.url.path in ADMIN_PAGE_PATHS


def _signature(secret: str, payload: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_session_token(secret: str, *, now: float | None = None) -> str:
    expires_at = int((time.time() if now is None else now) + SESSION_TTL_SECONDS)
    payload = str(expires_at)
    return f"{payload}.{_signature(secret, payload)}"


def session_token_is_valid(token: str, secret: str, *, now: float | None = None) -> bool:
    if not secret or not token or "." not in token:
        return False
    payload, _, signature = token.partition(".")
    if not payload.isdigit():
        return False
    if not hmac.compare_digest(signature, _signature(secret, payload)):
        return False
    return int(payload) > int(time.time() if now is None else now)


def api_key_matches(supplied: str, secret: str) -> bool:
    """自动化脚本用的 `X-Admin-Key`。"""
    if not secret or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))


def credentials_match(username: str, password: str, secret: str) -> bool:
    """固定账号 `admin` + `ADMIN_API_KEY` 作为密码。"""
    if not secret:
        return False
    if not hmac.compare_digest(username, ADMIN_USERNAME):
        return False
    return hmac.compare_digest(password.encode("utf-8"), secret.encode("utf-8"))


def basic_credentials_match(request: Request, secret: str) -> bool:
    """兼容旧版 Basic 认证（用户名 admin，密码同管理密钥）。"""
    if not secret:
        return False
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(
            authorization.removeprefix("Basic "), validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    return credentials_match(username, password, secret)


def is_cross_site(request: Request) -> bool:
    """浏览器凭据发起的写请求必须来自同源。

    `X-Admin-Key` 是脚本凭据，不参与来源判定；会话 Cookie 与 Basic 属于浏览器
    凭据，写入前都要过这一关。
    """
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    source = origin or referer
    parsed = urlparse(source or "")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return True
    return source is not None and (
        parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc
    )


def safe_next_path(target: str | None) -> str:
    """只接受站内相对路径，避免登录跳转变成开放重定向。"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/admin"
    if "\\" in target or "\r" in target or "\n" in target:
        return "/admin"
    return target
