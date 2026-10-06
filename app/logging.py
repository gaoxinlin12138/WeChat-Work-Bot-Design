import logging
import re

import structlog

_SENSITIVE_QUERY_RE = re.compile(r"(?i)(\b(?:corpsecret|access_token)=)[^&\s\"']+")
_SENSITIVE_JSON_RE = re.compile(
    r"(?i)([\"'](?:corpsecret|access_token)[\"']\s*:\s*[\"'])[^\"']*"
)


def redact_log_message(value: object) -> str:
    """Remove reusable WeCom credentials before a record reaches any handler."""
    text = str(value)
    text = _SENSITIVE_QUERY_RE.sub(r"\1[redacted]", text)
    return _SENSITIVE_JSON_RE.sub(r"\1[redacted]", text)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_message(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    # httpx INFO records include the full URL. WeCom's directory API must put its
    # credential in the query string, so keep transport details out of normal logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
