"""Privacy-safe identifiers for LangGraph threads and idempotent requests."""

from hashlib import sha256
from uuid import uuid4


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def build_thread_id(
    conversation_id: str,
    *,
    corp_id: str | None = None,
    bot_id: str | None = None,
) -> str:
    """Scope a conversation to one enterprise and bot without storing user PII."""
    scope = f"{corp_id or 'default-corp'}:{bot_id or 'default-bot'}:{conversation_id}"
    return f"wecom:{_fingerprint(scope)}"


def build_request_id(request_id: str | None) -> str:
    """Return a deterministic fingerprint, or a unique id for non-WeCom callers."""
    if request_id and request_id.strip():
        return f"wecom-message:{_fingerprint(request_id.strip())}"
    return f"internal:{uuid4().hex}"
