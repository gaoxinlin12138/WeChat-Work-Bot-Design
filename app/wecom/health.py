import asyncio
import json
from datetime import UTC, datetime

import redis
import redis.asyncio as async_redis
import structlog

from app.config import Settings

logger = structlog.get_logger()

WECOM_HEALTH_KEY = "wecom:long_connection:health"
WECOM_HEALTH_TTL_SECONDS = 90
WECOM_HEARTBEAT_INTERVAL_SECONDS = 20


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connection_url(redis_url: str) -> str:
    # Windows Proactor may prefer IPv6 for localhost while the bundled Redis
    # listens only on IPv4, causing an otherwise healthy local service to time out.
    return redis_url.replace("://localhost", "://127.0.0.1", 1)


class WeComHealthReporter:
    """Publish authenticated long-connection state with an expiring heartbeat."""

    def __init__(self, settings: Settings) -> None:
        self.redis_url = settings.redis_url
        self.status = "starting"
        self.authenticated_at: str | None = None

    async def publish(self, status: str, *, reason: str | None = None) -> None:
        self.status = status
        if status == "online" and self.authenticated_at is None:
            self.authenticated_at = _now_iso()
        if not self.redis_url:
            return
        payload = {
            "status": status,
            "updated_at": _now_iso(),
            "authenticated_at": self.authenticated_at,
        }
        if reason:
            payload["reason"] = str(reason)[:200]
        client = async_redis.Redis.from_url(
            _connection_url(self.redis_url),
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            await client.set(
                WECOM_HEALTH_KEY,
                json.dumps(payload, ensure_ascii=False),
                ex=WECOM_HEALTH_TTL_SECONDS,
            )
        except Exception as exc:
            await logger.awarning(
                "wecom_health_publish_failed", error_type=type(exc).__name__
            )
        finally:
            await client.aclose()

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(WECOM_HEARTBEAT_INTERVAL_SECONDS)
            if self.status == "online":
                await self.publish("online")


def read_wecom_connection_status(settings: Settings) -> str:
    """Return online only when a recently authenticated process is heartbeating."""
    if not settings.wecom_bot_id or not settings.wecom_bot_secret:
        return "not_configured"
    if not settings.redis_url:
        return "unavailable"
    client = redis.Redis.from_url(
        _connection_url(settings.redis_url),
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        raw = client.get(WECOM_HEALTH_KEY)
        if not raw:
            return "offline"
        payload = json.loads(raw)
        return "online" if payload.get("status") == "online" else "offline"
    except Exception:
        return "unavailable"
    finally:
        client.close()
