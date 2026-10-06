from fastapi.testclient import TestClient

import app.main as main_module
import app.wecom.health as wecom_health
from app.config import Settings
from app.main import app


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "wecom-knowledge-bot",
        "environment": "test",
    }


def test_readiness_reports_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "check_dependencies",
        lambda: {
            "postgresql": "ok",
            "redis": "ok",
            "llm": "configured",
            "embedding": "configured",
            "wecom": "online",
        },
    )
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_is_degraded_when_dependency_is_down(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "check_dependencies",
        lambda: {"postgresql": "unavailable", "redis": "ok"},
    )
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_wecom_health_requires_authenticated_heartbeat(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self, value):
            self.value = value

        def get(self, _key):
            return self.value

        def close(self):
            return None

    settings = Settings(
        _env_file=None,
        redis_url="redis://localhost:6379/0",
        wecom_bot_id="bot",
        wecom_bot_secret="secret",
    )
    monkeypatch.setattr(
        wecom_health.redis.Redis,
        "from_url",
        lambda *args, **kwargs: FakeRedis(b'{"status":"online"}'),
    )
    assert wecom_health.read_wecom_connection_status(settings) == "online"

    monkeypatch.setattr(
        wecom_health.redis.Redis,
        "from_url",
        lambda *args, **kwargs: FakeRedis(None),
    )
    assert wecom_health.read_wecom_connection_status(settings) == "offline"
