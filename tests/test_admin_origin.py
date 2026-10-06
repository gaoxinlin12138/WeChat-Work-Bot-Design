from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


def test_basic_login_rejects_cross_site_even_with_invalid_api_key(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(app_env="production", admin_api_key="fixture-secret"),
    )
    client = TestClient(app)
    response = client.post(
        "/business/does-not-exist",
        auth=("admin", "fixture-secret"),
        headers={"Origin": "https://foreign.example", "X-Admin-Key": "invalid"},
    )
    assert response.status_code == 403


def test_basic_login_rejects_null_origin(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(app_env="production", admin_api_key="fixture-secret"),
    )
    response = TestClient(app).post(
        "/business/does-not-exist",
        auth=("admin", "fixture-secret"),
        headers={"Origin": "null"},
    )
    assert response.status_code == 403


def test_same_origin_and_valid_api_key_requests_pass_authentication(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(app_env="production", admin_api_key="fixture-secret"),
    )
    client = TestClient(app)
    same_origin = client.post(
        "/business/does-not-exist",
        auth=("admin", "fixture-secret"),
        headers={"Origin": "http://testserver"},
    )
    api_key = client.post(
        "/business/does-not-exist",
        headers={"X-Admin-Key": "fixture-secret"},
    )
    assert same_origin.status_code == api_key.status_code == 404
