"""管理台登录页与会话 Cookie 的行为约束。

方案：浏览器不再走原生 Basic 弹窗，而是访问 `/admin/login` 表单，用固定账号
`admin` + `ADMIN_API_KEY` 换一个 HMAC 签名 Cookie；`X-Admin-Key` 继续给自动化用。
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.admin.router as admin_router
import app.main as main_module
from app.admin import auth
from app.main import app


def _production_client(monkeypatch, key: str = "fixture-secret") -> TestClient:
    settings = SimpleNamespace(app_env="production", admin_api_key=key)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(admin_router, "get_settings", lambda: settings)
    return TestClient(app)


def _login(client: TestClient, password: str = "fixture-secret", next_path: str = "/admin"):
    return client.post(
        "/admin/login",
        data={"username": "admin", "password": password, "next": next_path},
        follow_redirects=False,
    )


def test_login_page_is_public_and_renders_the_form(monkeypatch) -> None:
    client = _production_client(monkeypatch)

    page = client.get("/admin/login")
    stylesheet = client.get("/admin-static/login.css")

    assert page.status_code == 200
    assert "管理员登录" in page.text
    assert 'name="password"' in page.text
    assert 'action="/admin/login"' in page.text
    assert stylesheet.status_code == 200
    assert ".login-panel" in stylesheet.text


def test_login_page_does_not_disclose_credentials_or_operations(monkeypatch) -> None:
    """登录页对未认证访问者是公开的，因此不能交代凭据位置与运维细节。

    早先版本把“密码即 .env 中的 ADMIN_API_KEY，可用 configure_production 生成”
    直接写在页面上，等于向任何能打开这个地址的人说明口令去哪找。
    """
    page = _production_client(monkeypatch).get("/admin/login").text

    for leaked in ("ADMIN_API_KEY", "configure_production", ".env", "X-Admin-Key"):
        assert leaked not in page
    # 账号也不预填，避免把固定账号名直接展示出来。
    assert 'value="admin"' not in page


def test_login_page_redirects_when_authentication_is_disabled(monkeypatch) -> None:
    settings = SimpleNamespace(app_env="development", admin_api_key="fixture-secret")
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(admin_router, "get_settings", lambda: settings)

    response = TestClient(app).get("/admin/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_correct_credentials_issue_hardened_session_cookie(monkeypatch) -> None:
    response = _login(_production_client(monkeypatch))

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    cookie_header = response.headers["set-cookie"].lower()
    assert auth.SESSION_COOKIE in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert auth.session_token_is_valid(
        response.cookies.get(auth.SESSION_COOKIE, ""), "fixture-secret"
    )


def test_session_cookie_grants_console_access_without_basic_auth(monkeypatch) -> None:
    client = _production_client(monkeypatch)

    before = client.get("/openapi.json", follow_redirects=False)
    _login(client)
    after = client.get("/openapi.json", follow_redirects=False)
    console = client.get("/admin")

    assert before.status_code == 401
    assert after.status_code == 200
    assert console.status_code == 200
    assert "知识运营中枢" in console.text


def test_wrong_password_returns_to_login_without_cookie(monkeypatch) -> None:
    response = _login(_production_client(monkeypatch), password="wrong")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?error=credentials"
    assert auth.SESSION_COOKIE not in response.cookies


def test_wrong_username_is_rejected(monkeypatch) -> None:
    client = _production_client(monkeypatch)

    response = client.post(
        "/admin/login",
        data={"username": "root", "password": "fixture-secret", "next": "/admin"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?error=credentials"


def test_failed_login_keeps_the_requested_destination(monkeypatch) -> None:
    response = _login(_production_client(monkeypatch), password="wrong", next_path="/docs")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?error=credentials&next=/docs"


def test_missing_admin_key_is_reported_instead_of_silently_failing(monkeypatch) -> None:
    response = _login(_production_client(monkeypatch, key=""))

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?error=unconfigured"


def test_logout_clears_the_session(monkeypatch) -> None:
    client = _production_client(monkeypatch)
    _login(client)
    assert client.get("/openapi.json", follow_redirects=False).status_code == 200

    logout = client.get("/admin/logout", follow_redirects=False)
    after = client.get("/openapi.json", follow_redirects=False)
    console = client.get("/admin", follow_redirects=False)

    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    assert after.status_code == 401
    assert console.status_code == 303


def test_cross_site_login_post_is_rejected(monkeypatch) -> None:
    client = _production_client(monkeypatch)

    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "fixture-secret", "next": "/admin"},
        headers={"Origin": "https://foreign.example"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?error=origin"
    assert auth.SESSION_COOKIE not in response.cookies


def test_cross_site_write_with_session_cookie_is_rejected(monkeypatch) -> None:
    client = _production_client(monkeypatch)
    _login(client)

    response = client.post(
        "/business/does-not-exist",
        headers={"Origin": "https://foreign.example"},
        follow_redirects=False,
    )
    same_origin = client.post(
        "/business/does-not-exist",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert same_origin.status_code == 404


def test_api_key_requests_still_bypass_the_browser_session(monkeypatch) -> None:
    client = _production_client(monkeypatch)

    response = client.get(
        "/openapi.json", headers={"X-Admin-Key": "fixture-secret"}, follow_redirects=False
    )
    rejected = client.get(
        "/openapi.json", headers={"X-Admin-Key": "wrong"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert rejected.status_code == 401


def test_session_token_signature_and_expiry_are_enforced() -> None:
    token = auth.create_session_token("secret")

    assert auth.session_token_is_valid(token, "secret")
    assert not auth.session_token_is_valid(token, "other-secret")
    assert not auth.session_token_is_valid(f"{token}x", "secret")
    assert not auth.session_token_is_valid("not-a-token", "secret")
    assert not auth.session_token_is_valid("", "secret")
    assert not auth.session_token_is_valid(token, "")
    # 过期令牌必须失效；同一个签发逻辑往前推 13 小时即已超时。
    expired = auth.create_session_token("secret", now=0)
    assert not auth.session_token_is_valid(expired, "secret")


def test_login_redirect_target_must_stay_on_site(monkeypatch) -> None:
    assert auth.safe_next_path(None) == "/admin"
    assert auth.safe_next_path("/knowledge") == "/knowledge"
    assert auth.safe_next_path("//evil.example/x") == "/admin"
    assert auth.safe_next_path("https://evil.example/x") == "/admin"
    assert auth.safe_next_path("/a\\b") == "/admin"

    response = _login(_production_client(monkeypatch), next_path="https://evil.example/x")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
