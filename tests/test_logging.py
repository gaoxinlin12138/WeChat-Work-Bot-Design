import logging

from app.logging import SensitiveDataFilter, redact_log_message


def test_sensitive_wecom_credentials_are_redacted() -> None:
    message = (
        "GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=corp"
        "&corpsecret=long-secret&access_token=short-token"
    )
    redacted = redact_log_message(message)
    assert "long-secret" not in redacted
    assert "short-token" not in redacted
    assert "corpsecret=[redacted]" in redacted
    assert "access_token=[redacted]" in redacted


def test_sensitive_filter_redacts_formatted_record_arguments() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "request %s",
        ("https://example.test/?access_token=temporary",),
        None,
    )
    assert SensitiveDataFilter().filter(record)
    assert "temporary" not in record.getMessage()
