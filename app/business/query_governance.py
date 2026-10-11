"""安全查询与回答输出治理。

模型只提出结构化意图；本模块把可执行能力、分页和展示约束固定在服务端。
它不接收 SQL，也不把字段/函数/工具名暴露给模型作为可执行入口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.ontology.schema import PRODUCT_FIELD_CATALOG


@dataclass(frozen=True)
class QueryCapability:
    operation: str
    description: str
    max_page_size: int
    allowed_roles: frozenset[str] = frozenset()
    aggregate_kinds: frozenset[str] = frozenset()
    max_items: int = 5


CAPABILITY_REGISTRY: dict[str, QueryCapability] = {
    "count": QueryCapability("count", "统计产品实体数量", 1, frozenset(), max_items=1),
    "aggregate": QueryCapability(
        "aggregate",
        "对受支持字段做去重计数或数值聚合",
        10,
        frozenset({"dimension", "measure"}),
        frozenset({"distinct_count", "max", "min", "average"}),
        max_items=10,
    ),
    "list": QueryCapability(
        "list", "分页列出产品", 5, frozenset(PRODUCT_FIELD_CATALOG), max_items=5
    ),
    "search": QueryCapability(
        "search", "按白名单条件检索产品", 5, frozenset(PRODUCT_FIELD_CATALOG), max_items=5
    ),
    "detail": QueryCapability(
        "detail", "查看指定产品详情", 1, frozenset(PRODUCT_FIELD_CATALOG), max_items=1
    ),
    "compare": QueryCapability(
        "compare", "比较最多三款产品", 3, frozenset(PRODUCT_FIELD_CATALOG), max_items=3
    ),
    "recommend": QueryCapability(
        "recommend", "按白名单条件推荐产品", 10, frozenset(PRODUCT_FIELD_CATALOG), max_items=10
    ),
    "media": QueryCapability("media", "列出或发送产品媒体", 5, frozenset({"media"}), max_items=5),
}


def capability_for(operation: str) -> QueryCapability | None:
    return CAPABILITY_REGISTRY.get(operation)


@dataclass(frozen=True)
class GovernanceIssue:
    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class PermissionContext:
    """Identity-derived permissions; never populated from the employee message."""

    subject_id: str
    scopes: frozenset[str] = frozenset()
    source: str = "identity"

    def allows(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes

    @property
    def can_view_sensitive(self) -> bool:
        return self.allows("product.cost.read")


@dataclass(frozen=True)
class QueryBuilder:
    """A tiny structured builder; SQL is intentionally outside this API."""

    operation: str
    fields: tuple[str, ...] = ()
    filters: tuple[Mapping[str, object], ...] = ()
    page: int = 1
    page_size: int = 5

    def validate(self, *, can_view_sensitive: bool = False) -> tuple[GovernanceIssue, ...]:
        return validate_capability(
            operation=self.operation,
            fields=self.fields,
            page_size=self.page_size,
            can_view_sensitive=can_view_sensitive,
        )

    def to_plan(self) -> dict[str, object]:
        issues = self.validate()
        if issues:
            raise ValueError(issues[0].message)
        return {
            "operation": self.operation,
            "fields": list(self.fields),
            "filters": [dict(item) for item in self.filters],
            "page": max(1, self.page),
            "page_size": self.page_size,
        }


def validate_capability(
    *,
    operation: str,
    fields: tuple[str, ...] = (),
    aggregate_kind: str | None = None,
    reference_count: int = 0,
    page_size: int = 1,
    can_view_sensitive: bool | PermissionContext = False,
) -> tuple[GovernanceIssue, ...]:
    """Validate a structured plan against server-owned capabilities."""
    if isinstance(can_view_sensitive, PermissionContext):
        can_view_sensitive = can_view_sensitive.can_view_sensitive
    capability = capability_for(operation)
    issues: list[GovernanceIssue] = []
    if capability is None:
        return (GovernanceIssue("unsupported_operation", "不支持的查询操作"),)
    if page_size < 1 or page_size > capability.max_page_size:
        issues.append(GovernanceIssue("page_size_exceeded", "分页大小超过服务端上限"))
    if operation == "compare" and not 1 <= reference_count <= 3:
        issues.append(GovernanceIssue("compare_limit", "比较最多支持三款产品"))
    if aggregate_kind and aggregate_kind not in capability.aggregate_kinds:
        issues.append(GovernanceIssue("aggregate_kind", "不支持的聚合类型"))
    for field_name in fields:
        definition = PRODUCT_FIELD_CATALOG.get(field_name)
        if definition is None:
            issues.append(GovernanceIssue("unknown_field", "字段不在能力白名单中", field_name))
            continue
        if operation == "aggregate" and definition.role not in capability.allowed_roles:
            issues.append(GovernanceIssue("field_not_aggregatable", "字段不能用于聚合", field_name))
        if (
            operation == "aggregate"
            and aggregate_kind in {"max", "min", "average"}
            and definition.role != "measure"
        ):
            issues.append(
                GovernanceIssue(
                    "measure_required",
                    "最大值、最小值和平均值只适用于度量字段",
                    field_name,
                )
            )
        if operation == "media" and definition.role != "media":
            issues.append(
                GovernanceIssue("media_field_required", "媒体查询只能访问媒体字段", field_name)
            )
        if operation == "count":
            issues.append(
                GovernanceIssue("count_target_only", "产品数量统计不接受字段聚合", field_name)
            )
        if definition.sensitive and not can_view_sensitive:
            issues.append(GovernanceIssue("permission_denied", "字段需要额外权限", field_name))
    return tuple(issues)


_UNSAFE_KEY = re.compile(
    r"(?:sql|table|column|schema|function|callable|tool|exec|where|select)", re.I
)
_UNSAFE_VALUE = re.compile(
    r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|;|--|/\*)", re.I
)


def reject_unsafe_model_payload(value: object) -> None:
    """Reject SQL/tool injection attempts before a model plan is parsed.

    The planner payload is data, never an execution language. Unknown keys are ignored by
    the parser, but dangerous keys/values are rejected so they cannot silently become an
    alternate execution channel.
    """
    def walk(node: object, path: str = "payload") -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                key_text = str(key)
                if _UNSAFE_KEY.search(key_text):
                    raise ValueError(f"模型计划包含受禁止的执行字段：{path}.{key_text}")
                walk(item, f"{path}.{key_text}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str) and _UNSAFE_VALUE.search(node):
            raise ValueError("模型计划包含疑似 SQL 或命令片段")
    walk(value)


@dataclass(frozen=True)
class QueryResult:
    """Transport-neutral result contract shared by executors and renderers."""

    items: tuple[Mapping[str, object], ...] = ()
    total_count: int = 0
    page: int = 1
    page_size: int = 5
    has_more: bool = False
    cursor: str | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    requested_fields: tuple[str, ...] = ()
    operation: str = "detail"
    data_source: str = "product_catalog"

    @property
    def returned_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class ResponseEnvelope:
    """Stable response contract; model text is never trusted to bypass it."""

    request_id: str
    status: str
    content: str
    result: QueryResult | None = None
    evidence: tuple[Mapping[str, object], ...] = ()
    context_mode: str = "independent"
    ambiguity: tuple[str, ...] = ()
    permission: Mapping[str, object] = field(default_factory=dict)
    validation: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result = self.result
        return {
            "request_id": self.request_id,
            "status": self.status,
            "content": self.content,
            "total_count": result.total_count if result else None,
            "items": [dict(item) for item in result.items] if result else [],
            "page": result.page if result else 1,
            "page_size": result.page_size if result else 0,
            "has_more": result.has_more if result else False,
            "cursor": result.cursor if result else None,
            "truncated": result.truncated if result else False,
            "truncation_reason": result.truncation_reason if result else None,
            "requested_fields": list(result.requested_fields) if result else [],
            "evidence": [dict(item) for item in self.evidence],
            "context_mode": self.context_mode,
            "ambiguity": list(self.ambiguity),
            "permission": dict(self.permission),
            "validation": dict(self.validation),
            "metadata": dict(self.metadata),
        }


def query_result_from_store(
    result: Any, *, operation: str, requested_fields: tuple[str, ...] = ()
) -> QueryResult:
    total = max(0, int(getattr(result, "total", 0)))
    page = max(1, int(getattr(result, "page", 1)))
    size = max(1, int(getattr(result, "page_size", 5)))
    items = tuple(item for item in getattr(result, "items", ()) if isinstance(item, Mapping))
    return QueryResult(
        items=items,
        total_count=total,
        page=page,
        page_size=size,
        has_more=page * size < total,
        requested_fields=requested_fields,
        operation=operation,
    )


class CursorCodec:
    """Short-lived, user/query-bound cursors; payload is signed and opaque."""

    def __init__(self, secret: str | None = None, ttl_seconds: int = 900) -> None:
        raw = secret or os.environ.get("WECom_QUERY_CURSOR_SECRET") or "local-query-cursor-secret"
        self._secret = raw.encode("utf-8")
        self.ttl_seconds = max(60, min(86400, int(ttl_seconds)))

    def encode(self, *, user_id: str, query_fingerprint: str, page: int) -> str:
        payload = {
            "u": user_id,
            "q": query_fingerprint,
            "p": max(1, int(page)),
            "e": int(time.time()) + self.ttl_seconds,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{body}.{sig}"

    def decode(self, token: str, *, user_id: str, query_fingerprint: str) -> int | None:
        try:
            body, signature = token.split(".", 1)
            expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]
            if not hmac.compare_digest(signature, expected):
                return None
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            payload = json.loads(raw)
            if payload.get("u") != user_id or payload.get("q") != query_fingerprint:
                return None
            if int(payload.get("e", 0)) < int(time.time()):
                return None
            return max(1, int(payload["p"]))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            return None


def query_fingerprint(plan: Mapping[str, object]) -> str:
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class OutputPolicy:
    list_page_size: int = 5
    aggregate_page_size: int = 10
    compare_limit: int = 3
    media_limit: int = 5
    max_chars: int = 6000
    field_max_chars: int = 300


def enforce_output_budget(
    content: str, *, policy: OutputPolicy | None = None
) -> tuple[str, bool, str | None]:
    """Apply deterministic character limits without asking a model to summarize."""
    selected = policy or OutputPolicy()
    normalized = "\n".join(line[: selected.field_max_chars] for line in str(content).splitlines())
    if len(normalized) <= selected.max_chars:
        return normalized, False, None
    clipped = normalized[: max(0, selected.max_chars - 34)].rstrip()
    return clipped + "\n内容较长，已截断；可回复“下一页”或“查看详情”。", True, "character_budget"


def render_envelope(
    envelope: ResponseEnvelope, *, policy: OutputPolicy | None = None
) -> ResponseEnvelope:
    content, truncated, reason = enforce_output_budget(envelope.content, policy=policy)
    result = envelope.result
    if result is not None and (truncated or result.truncated):
        result = QueryResult(
            **{
                **result.__dict__,
                "truncated": True,
                "truncation_reason": reason or result.truncation_reason,
            }
        )
    if result is not None and result.has_more and "下一页" not in content:
        content = f"{content}\n回复“下一页”继续查看。"
    return ResponseEnvelope(
        request_id=envelope.request_id,
        status=envelope.status,
        content=content,
        result=result,
        evidence=envelope.evidence,
        context_mode=envelope.context_mode,
        ambiguity=envelope.ambiguity,
        permission=envelope.permission,
        validation=envelope.validation,
        metadata=envelope.metadata,
    )


def validate_response_envelope(envelope: ResponseEnvelope) -> tuple[GovernanceIssue, ...]:
    """Check result metadata before an envelope is emitted or audited."""
    if envelope.result is None:
        return ()
    result = envelope.result
    issues: list[GovernanceIssue] = []
    if result.total_count < 0 or result.page < 1 or result.page_size < 1:
        issues.append(GovernanceIssue("result_shape", "结果分页元数据无效"))
    if result.returned_count > result.page_size and result.operation not in {"count", "aggregate"}:
        issues.append(GovernanceIssue("result_overflow", "返回条数超过分页上限"))
    expected_more = result.page * result.page_size < result.total_count
    if result.operation != "count" and result.has_more != expected_more:
        issues.append(GovernanceIssue("pagination_inconsistent", "has_more 与总数/页码不一致"))
    if result.truncated and not result.truncation_reason:
        issues.append(GovernanceIssue("truncation_reason_missing", "截断结果缺少原因"))
    return tuple(issues)

