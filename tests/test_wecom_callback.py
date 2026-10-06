import base64
import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.wecom.crypto import WeComCrypto, calculate_signature

TOKEN = "test-token"
CORP_ID = "ww-test-corp"
AES_KEY = base64.b64encode(os.urandom(32)).decode("ascii")[:-1]


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch) -> WeComCrypto:
    monkeypatch.setenv("WECOM_CORP_ID", CORP_ID)
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("WECOM_ENCODING_AES_KEY", AES_KEY)
    get_settings.cache_clear()
    return WeComCrypto(TOKEN, AES_KEY, CORP_ID)


def test_callback_url_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    crypto = _configure(monkeypatch)
    timestamp = "1700000000"
    nonce = "nonce"
    encrypted_echo = crypto.encrypt("verified")
    signature = calculate_signature(TOKEN, timestamp, nonce, encrypted_echo)

    with TestClient(app) as client:
        response = client.get(
            "/wxwork/callback",
            params={
                "msg_signature": signature,
                "timestamp": timestamp,
                "nonce": nonce,
                "echostr": encrypted_echo,
            },
        )

    assert response.status_code == 200
    assert response.text == "verified"


def test_encrypted_text_message_returns_encrypted_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crypto = _configure(monkeypatch)
    timestamp = "1700000001"
    nonce = "nonce-2"
    plain_message = (
        "<xml><ToUserName><![CDATA[corp]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1700000001</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[你好]]></Content><MsgId>123</MsgId></xml>"
    )
    encrypted = crypto.encrypt(plain_message)
    signature = calculate_signature(TOKEN, timestamp, nonce, encrypted)
    outer_xml = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"

    with TestClient(app) as client:
        response = client.post(
            "/wxwork/callback",
            params={
                "msg_signature": signature,
                "timestamp": timestamp,
                "nonce": nonce,
            },
            content=outer_xml,
        )

    assert response.status_code == 200
    assert "<Encrypt>" in response.text
    response_encrypted = response.text.split("<Encrypt><![CDATA[", 1)[1].split("]]>", 1)[0]
    decrypted_reply = crypto.decrypt(response_encrypted)
    assert "联调成功，收到：你好" in decrypted_reply
    assert "zhangsan" in decrypted_reply


def test_invalid_signature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    crypto = _configure(monkeypatch)
    encrypted_echo = crypto.encrypt("verified")

    with TestClient(app) as client:
        response = client.get(
            "/wxwork/callback",
            params={
                "msg_signature": "invalid",
                "timestamp": "1700000000",
                "nonce": "nonce",
                "echostr": encrypted_echo,
            },
        )

    assert response.status_code == 403


def test_missing_configuration_returns_service_unavailable() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/wxwork/callback",
            params={
                "msg_signature": "unused",
                "timestamp": "1700000000",
                "nonce": "nonce",
                "echostr": "unused",
            },
        )

    assert response.status_code == 503
