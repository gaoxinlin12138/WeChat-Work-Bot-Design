import importlib.util
import json
from pathlib import Path


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "一键启动项目.py"
    spec = importlib.util.spec_from_file_location("project_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_launcher_reports_wecom_online_only_after_ready_heartbeat(monkeypatch) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {"status": "ready", "dependencies": {"wecom": "online"}}
        ),
    )

    assert launcher._wecom_online() is True


def test_launcher_does_not_treat_api_only_as_wecom_ready(monkeypatch) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {"status": "degraded", "dependencies": {"wecom": "offline"}},
            status=503,
        ),
    )

    assert launcher._wecom_online() is False
