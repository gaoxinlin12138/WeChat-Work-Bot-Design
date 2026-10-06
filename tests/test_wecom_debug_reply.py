from pathlib import Path


def test_long_connection_has_no_debug_echo_reply() -> None:
    source = Path("app/wecom/long_connection.py").read_text(encoding="utf-8")
    assert "联调成功，收到" not in source
