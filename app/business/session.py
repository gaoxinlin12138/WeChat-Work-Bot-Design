# ruff: noqa: E701,E501
import json
import time
from collections.abc import Iterator, MutableMapping
from typing import Any

import redis
import structlog

logger = structlog.get_logger()
_REDIS_RETRY_AT_BY_URL: dict[str, float] = {}
_REDIS_RETRY_SECONDS = 30.0


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value: str) -> Any:
    return json.loads(value)


class SharedState(MutableMapping[str, Any]):
    """Redis 优先、内存兜底的轻量状态映射。"""

    def __init__(self, url: str | None, prefix: str):
        self._prefix, self._memory = prefix, {}
        self._url = url or ""
        self._memory_expiry: dict[str, float] = {}
        self._ttl_seconds = 1800
        self._retry_at = 0.0
        self._redis_candidate = None
        try:
            self._redis = (
                redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                    retry_on_timeout=False,
                )
                if url
                else None
            )
            self._redis_candidate = self._redis
            if self._redis:
                blocked_until = _REDIS_RETRY_AT_BY_URL.get(self._url, 0.0)
                if time.monotonic() < blocked_until:
                    self._redis = None
                    self._retry_at = blocked_until
                else:
                    self._redis.ping()
        except Exception:
            self._redis = None
            self._retry_at = time.monotonic() + _REDIS_RETRY_SECONDS
            _REDIS_RETRY_AT_BY_URL[self._url] = self._retry_at
            logger.warning("redis_state_fallback_enabled", state_prefix=self._prefix)

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _disable_redis(self, operation: str, exc: Exception) -> None:
        self._redis = None
        self._retry_at = time.monotonic() + _REDIS_RETRY_SECONDS
        _REDIS_RETRY_AT_BY_URL[self._url] = self._retry_at
        logger.warning(
            "redis_state_fallback_enabled",
            state_prefix=self._prefix,
            operation=operation,
            error_type=type(exc).__name__,
        )

    def _recover_redis(self) -> None:
        retry_at = (
            max(self._retry_at, _REDIS_RETRY_AT_BY_URL.get(self._url, 0.0))
            if self._retry_at
            else 0.0
        )
        if self._redis or self._redis_candidate is None or time.monotonic() < retry_at:
            return
        try:
            self._redis_candidate.ping()
        except Exception:
            self._retry_at = time.monotonic() + _REDIS_RETRY_SECONDS
            _REDIS_RETRY_AT_BY_URL[self._url] = self._retry_at
            return
        self._redis = self._redis_candidate
        _REDIS_RETRY_AT_BY_URL.pop(self._url, None)
        # 网络恢复后以共享状态为准，不能把故障期间的本机旧状态覆盖其他 worker 的新选择。
        self._memory.clear()
        self._memory_expiry.clear()
        logger.info("redis_state_connection_restored", state_prefix=self._prefix)

    def _purge_expired_memory(self) -> None:
        now = time.monotonic()
        for key, expiry in list(self._memory_expiry.items()):
            if expiry <= now:
                self._memory_expiry.pop(key, None)
                self._memory.pop(key, None)

    def __getitem__(self, key: str) -> Any:
        self._recover_redis()
        if self._redis:
            try:
                value = self._redis.get(self._key(key))
            except Exception as exc:
                self._disable_redis("read", exc)
                value = None
            if value is not None:
                return decode(value) if isinstance(value, str) else value
            if self._redis:
                raise KeyError(key)
        self._purge_expired_memory()
        value = self._memory[key]
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self._recover_redis()
        if self._redis:
            try:
                payload = encode(value)
                self._redis.setex(self._key(key), 1800, payload)
                return
            except Exception as exc:
                self._disable_redis("write", exc)
        self._purge_expired_memory()
        self._memory[key] = value
        self._memory_expiry[key] = time.monotonic() + self._ttl_seconds

    def __delitem__(self, key: str) -> None:
        self._recover_redis()
        if self._redis:
            try:
                if not self._redis.delete(self._key(key)):
                    raise KeyError(key)
                return
            except KeyError:
                raise
            except Exception as exc:
                self._disable_redis("delete", exc)
        self._purge_expired_memory()
        del self._memory[key]
        self._memory_expiry.pop(key, None)

    def __iter__(self) -> Iterator[str]:
        self._recover_redis()
        if self._redis:
            try:
                keys = list(self._redis.scan_iter(f"{self._prefix}:*"))
                return (k[len(self._prefix) + 1 :] for k in keys)
            except Exception as exc:
                self._disable_redis("iterate", exc)
        self._purge_expired_memory()
        return iter(self._memory)

    def __len__(self) -> int:
        self._recover_redis()
        if self._redis:
            try:
                # dbsize() counts every application key in the Redis database;
                # count only this state's prefix instead.
                return sum(1 for _ in self._redis.scan_iter(f"{self._prefix}:*"))
            except Exception as exc:
                self._disable_redis("length", exc)
        self._purge_expired_memory()
        return len(self._memory)


class ReplySessionState(SharedState):
    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        try:
            from app.business.intent import ReplyScenario
            from app.business.tools.common import ReplySession

            if isinstance(value, dict) and "scenario" in value:
                payload = dict(value)
                payload["scenario"] = ReplyScenario(str(payload["scenario"]))
                return ReplySession(**payload)
            return value
        except Exception:
            return value

    def __setitem__(self, key: str, value: Any) -> None:
        if hasattr(value, "scenario") and hasattr(value, "collected"):
            value = {
                "scenario": value.scenario,
                "collected": value.collected,
                "last_seen": value.last_seen,
            }
        super().__setitem__(key, value)
