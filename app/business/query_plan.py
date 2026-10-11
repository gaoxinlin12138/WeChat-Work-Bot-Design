"""Unified QueryPlan contract for planning, execution review, and replay.

The existing planner proposes a :class:`ProductQuery`; this module turns that
proposal into a validated, permission-checked contract before the established
V2 executor runs. It remains independent from the database: models may propose
structured data, but never SQL or arbitrary tool calls.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from app.business.product_query import (
    ProductAggregate,
    ProductFieldRequest,
    ProductFilter,
    ProductQuery,
    ProductReference,
    ProductSort,
    field_evidence_is_valid,
    is_overview_request,
    validate_product_query,
)
from app.business.query_governance import (
    CAPABILITY_REGISTRY,
    PermissionContext,
    reject_unsafe_model_payload,
    validate_capability,
)
from app.ontology.schema import PRODUCT_FIELD_CATALOG
from app.ontology.values import matching_values

QUERY_PLAN_VERSION = 1
DOMAINS = frozenset({"policy", "product", "media", "script", "general"})
SHAPES = frozenset(
    {
        "detail",
        "detail_full",
        "listing",
        "count",
        "aggregate",
        "rank",
        "compare",
        "media",
        "policy",
        "script",
        "chitchat",
        "out_of_scope",
    }
)
OPERATIONS = frozenset(CAPABILITY_REGISTRY)
FILTER_OPERATORS = frozenset(
    {
        "equals",
        "contains",
        "is_present",
        "is_empty",
        "less_than",
        "less_than_or_equal",
        "greater_than",
        "greater_than_or_equal",
        "between",
        "in",
    }
)
CONTEXT_MODES = frozenset({"independent", "continuation"})
DELIVERY_MODES = frozenset({"text", "media", "text_and_media", "none"})

# Cross-domain registry.  The product field catalogue remains the source of
# truth for product fields; this table prevents a model from routing policy or
# published-script data through a product executor (or vice versa).
DOMAIN_CAPABILITY_REGISTRY: dict[str, dict[str, object]] = {
    "policy": {
        "shapes": frozenset({"policy"}),
        "delivery": frozenset({"text"}),
        "requires_evidence": True,
    },
    "product": {
        "shapes": frozenset(
            {"detail", "detail_full", "listing", "count", "aggregate", "rank", "compare"}
        ),
        "delivery": frozenset({"text"}),
        "requires_evidence": True,
    },
    "media": {
        "shapes": frozenset({"media"}),
        "delivery": frozenset({"media", "text_and_media"}),
        "requires_evidence": True,
    },
    "script": {
        "shapes": frozenset({"script"}),
        "delivery": frozenset({"text"}),
        "requires_evidence": True,
    },
    "general": {
        "shapes": frozenset({"chitchat", "out_of_scope"}),
        "delivery": frozenset({"text"}),
        "requires_evidence": False,
    },
}


@dataclass(frozen=True)
class PlanSubject:
    value: str
    kind: Literal["model", "context", "value", "all"] = "model"
    evidence: str = ""
    source: Literal["current", "context"] = "current"

    def to_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "kind": self.kind,
            "evidence": self.evidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class PlanField:
    name: str
    evidence: str = ""
    source: Literal["current", "context", "fallback"] = "current"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "evidence": self.evidence, "source": self.source}


@dataclass(frozen=True)
class PlanFilter:
    field: str
    operator: str
    value: object = ""
    evidence: str = ""
    source: Literal["current", "context", "fallback"] = "current"

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "operator": self.operator,
            "value": self.value,
            "evidence": self.evidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class PlanSort:
    field: str
    direction: Literal["asc", "desc"] = "asc"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "direction": self.direction}


@dataclass(frozen=True)
class QueryPlan:
    """The versioned contract accepted by production review and replay tools."""

    domain: str
    shape: str
    subjects: tuple[PlanSubject, ...] = ()
    fields: tuple[PlanField, ...] = ()
    filters: tuple[PlanFilter, ...] = ()
    aggregate: Mapping[str, object] | None = None
    sort: tuple[PlanSort, ...] = ()
    page: int = 1
    page_size: int = 5
    delivery: str = "text"
    context_mode: str = "independent"
    clarification: Mapping[str, object] | None = None
    version: int = QUERY_PLAN_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> QueryPlan:
        """Construct a canonical plan for trusted/server-produced JSON.

        Employee-message evidence is still checked by :func:`parse_query_plan`;
        this convenience method is intended for round-tripping audit records.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("QueryPlan 必须是对象")
        evidence = json.dumps(payload, ensure_ascii=False)
        checked = parse_query_plan(payload, evidence)
        if not checked.passed or checked.plan is None:
            raise ValueError(checked.issues[0].message if checked.issues else "QueryPlan 无效")
        return checked.plan

    @classmethod
    def from_json(cls, value: str) -> QueryPlan:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("QueryPlan JSON 无效") from exc
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "domain": self.domain,
            "shape": self.shape,
            "subjects": [item.to_dict() for item in self.subjects],
            "fields": [item.to_dict() for item in self.fields],
            "filters": [item.to_dict() for item in self.filters],
            "aggregate": dict(self.aggregate) if self.aggregate is not None else None,
            "sort": [item.to_dict() for item in self.sort],
            "page": self.page,
            "page_size": self.page_size,
            "delivery": self.delivery,
            "context_mode": self.context_mode,
            "clarification": dict(self.clarification) if self.clarification else None,
        }

    def json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def operation(self) -> str:
        return {"detail_full": "detail", "listing": "list", "rank": "search"}.get(
            self.shape, self.shape
        )


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str
    path: str = ""
    severity: Literal["error", "warning"] = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class PlanValidation:
    plan: QueryPlan | None
    issues: tuple[PlanIssue, ...] = ()
    corrected: bool = False

    @property
    def passed(self) -> bool:
        return self.plan is not None and not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "pass": self.passed,
            "issues": [item.to_dict() for item in self.issues],
            "plan": self.plan.to_dict() if self.plan else None,
            "corrected": self.corrected,
        }


@dataclass(frozen=True)
class PlanReview:
    passed: bool
    issues: tuple[PlanIssue, ...] = ()
    corrected_plan: QueryPlan | None = None
    correction_attempts: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pass": self.passed,
            "issues": [item.to_dict() for item in self.issues],
            "corrected_plan": self.corrected_plan.to_dict() if self.corrected_plan else None,
            "correction_attempts": self.correction_attempts,
        }


@dataclass(frozen=True)
class AnswerValidation:
    passed: bool
    issues: tuple[PlanIssue, ...] = ()
    checked_facts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "pass": self.passed,
            "issues": [item.to_dict() for item in self.issues],
            "checked_facts": list(self.checked_facts),
        }


@dataclass(frozen=True)
class ShadowComparison:
    case_id: str
    shape_match: bool
    subjects_match: bool
    fields_match: bool
    filters_match: bool
    permissions_match: bool = True
    answer_match: bool = True
    latency_ms: Mapping[str, float] = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all(
            (
                self.shape_match,
                self.subjects_match,
                self.fields_match,
                self.filters_match,
                self.permissions_match,
                self.answer_match,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "shape_match": self.shape_match,
            "subjects_match": self.subjects_match,
            "fields_match": self.fields_match,
            "filters_match": self.filters_match,
            "permissions_match": self.permissions_match,
            "answer_match": self.answer_match,
            "latency_ms": dict(self.latency_ms),
            "issues": list(self.issues),
            "passed": self.passed,
        }


def _issue(code: str, message: str, path: str = "") -> PlanIssue:
    return PlanIssue(code, message, path)


def _as_text(value: object, path: str, issues: list[PlanIssue], *, required: bool = False) -> str:
    if value is None:
        if required:
            issues.append(_issue("missing", "必填字段缺失", path))
        return ""
    if not isinstance(value, str):
        issues.append(_issue("type", "必须是字符串", path))
        return ""
    return value.strip()[:500]


def _parse_subjects(raw: object, issues: list[PlanIssue]) -> tuple[PlanSubject, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("type", "subjects 必须是数组", "subjects"))
        return ()
    result: list[PlanSubject] = []
    for index, item in enumerate(raw[:20]):
        path = f"subjects[{index}]"
        if isinstance(item, str):
            item = {"value": item, "evidence": item}
        if not isinstance(item, Mapping):
            issues.append(_issue("type", "subject 必须是对象", path))
            continue
        value = _as_text(item.get("value"), f"{path}.value", issues, required=True)
        kind = _as_text(item.get("kind") or "model", f"{path}.kind", issues)
        source = _as_text(item.get("source") or "current", f"{path}.source", issues)
        evidence = _as_text(item.get("evidence") or "", f"{path}.evidence", issues)
        if kind not in {"model", "context", "value", "all"}:
            issues.append(_issue("unsupported_subject_kind", "subject kind 不受支持", path))
        if source not in {"current", "context"}:
            issues.append(_issue("unsupported_source", "subject source 不受支持", path))
        if value:
            result.append(PlanSubject(value, kind or "model", evidence, source or "current"))  # type: ignore[arg-type]
    return tuple(result)


def _parse_fields(raw: object, issues: list[PlanIssue]) -> tuple[PlanField, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("type", "fields 必须是数组", "fields"))
        return ()
    result: list[PlanField] = []
    for index, item in enumerate(raw[:30]):
        path = f"fields[{index}]"
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, Mapping):
            issues.append(_issue("type", "field 必须是对象或字符串", path))
            continue
        name = _as_text(item.get("name", item.get("field")), f"{path}.name", issues, required=True)
        evidence = _as_text(item.get("evidence") or "", f"{path}.evidence", issues)
        source = _as_text(item.get("source") or "current", f"{path}.source", issues)
        if name:
            result.append(PlanField(name, evidence, source or "current"))  # type: ignore[arg-type]
    return tuple(dict.fromkeys(result))


def _parse_filters(raw: object, issues: list[PlanIssue]) -> tuple[PlanFilter, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("type", "filters 必须是数组", "filters"))
        return ()
    result: list[PlanFilter] = []
    for index, item in enumerate(raw[:30]):
        path = f"filters[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue("type", "filter 必须是对象", path))
            continue
        field_name = _as_text(item.get("field"), f"{path}.field", issues, required=True)
        operator = _as_text(item.get("operator") or "equals", f"{path}.operator", issues)
        value = item.get("value", "")
        evidence = _as_text(item.get("evidence") or "", f"{path}.evidence", issues)
        source = _as_text(item.get("source") or "current", f"{path}.source", issues)
        if operator not in FILTER_OPERATORS:
            issues.append(_issue("unsupported_operator", "筛选操作符不受支持", path))
        if source not in {"current", "context", "fallback"}:
            issues.append(_issue("unsupported_source", "filter source 不受支持", path))
        if field_name:
            result.append(PlanFilter(field_name, operator, value, evidence, source or "current"))  # type: ignore[arg-type]
    return tuple(result)


def _parse_sort(raw: object, issues: list[PlanIssue]) -> tuple[PlanSort, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("type", "sort 必须是数组", "sort"))
        return ()
    result: list[PlanSort] = []
    for index, item in enumerate(raw[:5]):
        path = f"sort[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue("type", "sort 必须是对象", path))
            continue
        name = _as_text(item.get("field"), f"{path}.field", issues, required=True)
        direction = _as_text(item.get("direction") or "asc", f"{path}.direction", issues)
        if direction not in {"asc", "desc"}:
            issues.append(_issue("unsupported_direction", "排序方向不受支持", path))
        if name:
            result.append(PlanSort(name, direction or "asc"))  # type: ignore[arg-type]
    return tuple(result)


def _check_evidence(evidence: str, message: str, path: str, issues: list[PlanIssue]) -> None:
    if not evidence or evidence.casefold().replace(" ", "") not in message.casefold().replace(
        " ", ""
    ):
        issues.append(_issue("ungrounded_evidence", "依据必须是员工原话中的连续片段", path))


def _validate_values(
    plan: QueryPlan,
    message: str,
    issues: list[PlanIssue],
    *,
    value_catalog: Mapping[str, Sequence[str]] | None = None,
    known_models: Sequence[str] | None = None,
) -> None:
    for index, subject in enumerate(plan.subjects):
        if subject.source == "current":
            _check_evidence(
                subject.evidence or subject.value, message, f"subjects[{index}]", issues
            )
        if known_models and subject.kind == "model" and subject.value not in known_models:
            issues.append(
                _issue("unknown_subject", "型号不在已发布产品目录中", f"subjects[{index}]")
            )
    for index, item in enumerate(plan.fields):
        definition = PRODUCT_FIELD_CATALOG.get(item.name)
        if definition is None:
            issues.append(_issue("unknown_field", "字段不在能力注册表中", f"fields[{index}]"))
            continue
        if item.source == "current":
            if not item.evidence or not field_evidence_is_valid(item.name, item.evidence, message):
                issues.append(
                    _issue("ungrounded_field", "字段依据不符合员工原话", f"fields[{index}]")
                )
    for index, item in enumerate(plan.filters):
        definition = PRODUCT_FIELD_CATALOG.get(item.field)
        if definition is None or not definition.filterable:
            issues.append(
                _issue("unknown_filter_field", "筛选字段不在能力注册表中", f"filters[{index}]")
            )
            continue
        if item.source == "current":
            _check_evidence(item.evidence, message, f"filters[{index}].evidence", issues)
            if (
                isinstance(item.value, str)
                and item.value
                and item.value.casefold() not in message.casefold()
            ):
                issues.append(
                    _issue(
                        "unquoted_filter_value",
                        "筛选值未出现在员工原话中",
                        f"filters[{index}].value",
                    )
                )
        if value_catalog and definition.value_type == "text" and isinstance(item.value, str):
            matches = matching_values(
                tuple(str(v) for v in value_catalog.get(item.field, ())), item.value
            )
            if not matches and item.operator in {"equals", "contains", "in"}:
                issues.append(
                    _issue(
                        "unverified_filter_value",
                        "筛选值不在已发布取值目录中",
                        f"filters[{index}].value",
                    )
                )


def _coerce_payload(payload: object) -> Mapping[str, object] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, Mapping) else None


def parse_query_plan(
    payload: object,
    message: str,
    *,
    value_catalog: Mapping[str, Sequence[str]] | None = None,
    known_models: Sequence[str] | None = None,
    permission: PermissionContext | None = None,
) -> PlanValidation:
    """Parse and validate a model JSON proposal without touching a database."""
    raw = _coerce_payload(payload)
    if raw is None:
        return PlanValidation(None, (_issue("invalid_json", "QueryPlan 必须是 JSON 对象"),))
    try:
        reject_unsafe_model_payload(raw)
    except ValueError as exc:
        return PlanValidation(None, (_issue("unsafe_payload", str(exc)),))
    allowed = {
        "version",
        "domain",
        "shape",
        "subjects",
        "fields",
        "filters",
        "aggregate",
        "sort",
        "page",
        "page_size",
        "delivery",
        "context_mode",
        "clarification",
    }
    unknown = sorted(set(str(key) for key in raw) - allowed)
    issues: list[PlanIssue] = [
        _issue("unknown_key", "QueryPlan 包含未注册字段", key) for key in unknown
    ]
    version = raw.get("version", QUERY_PLAN_VERSION)
    if version != QUERY_PLAN_VERSION:
        issues.append(_issue("unsupported_version", "QueryPlan 版本不受支持", "version"))
    domain = _as_text(raw.get("domain"), "domain", issues, required=True)
    shape = _as_text(raw.get("shape"), "shape", issues, required=True)
    if domain not in DOMAINS:
        issues.append(_issue("unsupported_domain", "domain 不受支持", "domain"))
    if shape not in SHAPES:
        issues.append(_issue("unsupported_shape", "shape 不受支持", "shape"))
    domain_spec = DOMAIN_CAPABILITY_REGISTRY.get(domain)
    if domain_spec and shape not in domain_spec["shapes"]:
        issues.append(_issue("shape_domain_mismatch", "shape 不属于该 domain", "shape"))
    subjects = _parse_subjects(raw.get("subjects", []), issues)
    fields = _parse_fields(raw.get("fields", []), issues)
    filters = _parse_filters(raw.get("filters", []), issues)
    sorts = _parse_sort(raw.get("sort", []), issues)
    aggregate = raw.get("aggregate")
    if aggregate is not None and not isinstance(aggregate, Mapping):
        issues.append(_issue("type", "aggregate 必须是对象或 null", "aggregate"))
        aggregate = None
    page = raw.get("page", 1)
    page_size = raw.get("page_size", 5)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        issues.append(_issue("invalid_page", "page 必须是正整数", "page"))
        page = 1
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        issues.append(_issue("invalid_page_size", "page_size 必须是正整数", "page_size"))
        page_size = 5
    delivery = _as_text(raw.get("delivery") or "text", "delivery", issues)
    context_mode = _as_text(raw.get("context_mode") or "independent", "context_mode", issues)
    clarification = raw.get("clarification")
    if clarification is not None and not isinstance(clarification, Mapping):
        issues.append(_issue("type", "clarification 必须是对象或 null", "clarification"))
        clarification = None
    if delivery not in DELIVERY_MODES:
        issues.append(_issue("unsupported_delivery", "delivery 不受支持", "delivery"))
    if domain_spec and delivery not in domain_spec["delivery"]:
        issues.append(_issue("delivery_domain_mismatch", "delivery 不属于该 domain", "delivery"))
    if context_mode not in CONTEXT_MODES:
        issues.append(_issue("unsupported_context_mode", "context_mode 不受支持", "context_mode"))
    plan = QueryPlan(
        domain=domain,
        shape=shape,
        subjects=subjects,
        fields=fields,
        filters=filters,
        aggregate=dict(aggregate) if isinstance(aggregate, Mapping) else None,
        sort=sorts,
        page=page,
        page_size=page_size,
        delivery=delivery or "text",
        context_mode=context_mode or "independent",
        clarification=dict(clarification) if isinstance(clarification, Mapping) else None,
        version=int(version) if isinstance(version, int) else QUERY_PLAN_VERSION,
    )
    if plan.domain in {"product", "media"}:
        _validate_values(
            plan, message, issues, value_catalog=value_catalog, known_models=known_models
        )
        capability_fields = tuple(item.name for item in plan.fields)
        if plan.aggregate and plan.aggregate.get("field"):
            capability_fields = (*capability_fields, str(plan.aggregate["field"]))
        capability_issues = validate_capability(
            operation=plan.operation,
            fields=capability_fields,
            aggregate_kind=(str(plan.aggregate.get("kind")) if plan.aggregate else None),
            reference_count=len(plan.subjects),
            page_size=plan.page_size,
            can_view_sensitive=permission or False,
        )
        issues.extend(_issue(item.code, item.message) for item in capability_issues)
        if plan.shape == "detail_full" and not plan.subjects:
            issues.append(_issue("missing_subject", "完整资料必须指定产品型号", "subjects"))
        if plan.shape == "detail" and plan.subjects and not plan.fields and not plan.clarification:
            issues.append(
                _issue(
                    "missing_fields", "详情查询缺少已验证字段，应澄清而不是扩展为完整资料", "fields"
                )
            )
        if plan.shape == "compare" and len(plan.subjects) < 2:
            issues.append(_issue("compare_subjects", "比较至少需要两个产品主体", "subjects"))
        if plan.shape == "media" and plan.delivery not in {"media", "text_and_media"}:
            issues.append(_issue("media_delivery", "媒体形状必须使用媒体交付", "delivery"))
        for index, item in enumerate(plan.sort):
            definition = PRODUCT_FIELD_CATALOG.get(item.field)
            if definition is None or not definition.can_sort:
                issues.append(
                    _issue("unknown_sort_field", "排序字段不在能力注册表中", f"sort[{index}]")
                )
        if plan.shape == "rank" and not plan.sort:
            issues.append(_issue("rank_sort_missing", "rank 形状必须指定排序字段", "sort"))
        if plan.shape == "aggregate" and plan.aggregate:
            kind = str(plan.aggregate.get("kind") or "")
            if kind not in {"distinct_count", "max", "min", "average"}:
                issues.append(_issue("aggregate_kind", "聚合类型不受支持", "aggregate.kind"))
        if plan.aggregate is not None and not plan.aggregate.get("field"):
            issues.append(
                _issue("aggregate_field_missing", "aggregate.field 必须存在", "aggregate.field")
            )
    return PlanValidation(
        plan if not any(item.severity == "error" for item in issues) else None,
        tuple(issues),
    )


def query_plan_from_product_query(query: ProductQuery, *, domain: str = "product") -> QueryPlan:
    if query.operation == "media" and domain == "product":
        domain = "media"
    shape = {
        "detail": "detail_full" if query.allow_overview and not query.fields else "detail",
        "list": "listing",
        "search": "listing",
        "count": "count",
        "aggregate": "aggregate",
        "compare": "compare",
        "recommend": "listing",
        "media": "media",
    }.get(query.operation, "detail")
    fields = tuple(
        PlanField(item.field, item.evidence, item.source)
        for item in query.effective_field_requests()
    )
    subjects = tuple(
        PlanSubject(
            item.value,
            "model",
            item.evidence,
            item.source if item.source != "fallback" else "current",
        )
        for item in query.effective_references()
    )
    filters = tuple(
        PlanFilter(item.field, item.operator, item.value, item.evidence, item.source)
        for item in query.filters
    )
    sort = tuple(PlanSort(item.field, item.direction) for item in query.sorts)
    aggregate = None
    if query.aggregate:
        aggregate = {
            "kind": query.aggregate.kind,
            "field": query.aggregate.field,
            "limit": query.aggregate.limit,
            "evidence": query.aggregate.evidence,
            "source": query.aggregate.source,
        }
    page_size = query.page_size
    if shape in {"detail", "detail_full", "count"}:
        page_size = 1
    elif shape == "compare":
        page_size = min(page_size, 3)
    elif shape == "aggregate":
        page_size = min(max(page_size, 1), 10)
    return QueryPlan(
        domain=domain,
        shape=shape,
        subjects=subjects,
        fields=fields,
        filters=filters,
        aggregate=aggregate,
        sort=sort,
        page=query.page,
        page_size=page_size,
        delivery="media" if query.operation == "media" else "text",
        context_mode=query.context_mode,
    )


def product_query_from_plan(
    plan: QueryPlan,
    message: str,
    *,
    can_view_sensitive: bool = False,
    base_query: ProductQuery | None = None,
) -> tuple[ProductQuery | None, PlanValidation]:
    """Bridge a validated unified plan into the existing deterministic executor."""
    if plan.domain not in {"product", "media"}:
        return None, PlanValidation(None, (_issue("wrong_domain", "该计划不是产品/媒体计划"),))
    op = plan.operation
    references = tuple(
        ProductReference(
            item.value,
            "exact" if item.kind == "model" else "context",
            item.evidence,
            item.source if item.source != "fallback" else "current",
        )
        for item in plan.subjects
    )
    requests = tuple(
        ProductFieldRequest(item.name, item.evidence, item.source) for item in plan.fields
    )
    filters = tuple(
        ProductFilter(item.field, item.operator, item.value, item.evidence, item.source)
        for item in plan.filters
    )
    sorts = tuple(ProductSort(item.field, item.direction) for item in plan.sort)
    aggregate = None
    if plan.aggregate:
        aggregate = ProductAggregate(
            kind=str(plan.aggregate.get("kind") or "distinct_count"),
            field=str(plan.aggregate.get("field") or ""),
            limit=max(1, min(50, int(plan.aggregate.get("limit") or 30))),
            evidence=str(plan.aggregate.get("evidence") or ""),
            source=str(plan.aggregate.get("source") or "current"),
        )
    query = ProductQuery(
        operation=op,  # type: ignore[arg-type]
        include_models=tuple(item.value for item in references),
        references=references,
        requested_fields=requests,
        fields=tuple(item.field for item in requests),
        filters=filters,
        sorts=sorts,
        aggregate=aggregate,
        page=plan.page,
        page_size=plan.page_size,
        context_mode=plan.context_mode,  # type: ignore[arg-type]
        allow_overview=plan.shape == "detail_full",
    )
    if base_query is not None:
        # Preserve trusted graph metadata that is intentionally outside the
        # public contract while replacing the canonical query fields.
        query = replace(
            base_query,
            operation=query.operation,
            include_models=query.include_models,
            references=query.references,
            requested_fields=query.requested_fields,
            fields=query.fields,
            filters=query.filters,
            sorts=query.sorts,
            aggregate=query.aggregate,
            page=query.page,
            page_size=query.page_size,
            context_mode=query.context_mode,
            allow_overview=query.allow_overview,
        )
    checked = validate_product_query(query, message, can_view_cost=can_view_sensitive)
    return checked.query, PlanValidation(
        query_plan_from_product_query(checked.query),
        tuple(
            _issue("product_validation", item) for item in (*checked.errors, *checked.ambiguities)
        ),
    )


def review_query_plan(
    plan: QueryPlan,
    message: str,
    *,
    permission: PermissionContext | None = None,
    value_catalog: Mapping[str, Sequence[str]] | None = None,
    correction_attempts: int = 0,
) -> PlanReview:
    """Review semantics once; corrections are deterministic and never invent facts."""
    issues: list[PlanIssue] = []
    validation = parse_query_plan(
        plan.to_dict(), message, value_catalog=value_catalog, permission=permission
    )
    issues.extend(validation.issues)
    corrected = plan
    attempts = correction_attempts
    # The common production failure is an overview request with an empty field
    # list.  The employee's own words determine this correction; no field/value
    # is fabricated.  Media words remain media and are never converted here.
    if (
        attempts < 1
        and plan.domain == "product"
        and plan.shape == "detail"
        and not plan.fields
        and plan.subjects
        and is_overview_request(
            message,
            tuple(
                ProductReference(item.value, "fragment", item.evidence, "current")
                for item in plan.subjects
            ),
        )
    ):
        corrected = replace(plan, shape="detail_full", page_size=min(plan.page_size, 1))
        attempts = 1
        issues = [
            item
            for item in issues
            if item.code not in {"ungrounded_field", "missing_subject", "missing_fields"}
        ]
    final = parse_query_plan(
        corrected.to_dict(), message, value_catalog=value_catalog, permission=permission
    )
    if final.passed:
        return PlanReview(True, tuple(issues), corrected if corrected != plan else None, attempts)
    return PlanReview(
        False, tuple(final.issues), corrected if corrected != plan else None, attempts
    )


def validate_answer(
    plan: QueryPlan,
    content: str,
    *,
    records: Sequence[Mapping[str, object]] = (),
    evidence: Sequence[Mapping[str, object]] = (),
    published_scripts: Sequence[str] = (),
) -> AnswerValidation:
    """Check coverage/provenance only; never rewrite a fact supplied by a store."""
    issues: list[PlanIssue] = []
    checked: list[str] = []
    if plan.domain in {"product", "media"}:
        if not records and plan.shape not in {"count", "aggregate"} and "没有" not in content:
            issues.append(_issue("missing_product_records", "产品答案缺少数据库结果依据"))
        for field_item in plan.fields:
            values = [
                str(row.get(field_item.name) or "")
                for row in records
                if row.get(field_item.name) not in (None, "")
            ]
            if values:
                checked.append(field_item.name)
                if not any(value in content for value in values):
                    issues.append(_issue("field_not_covered", f"答案未覆盖字段 {field_item.name}"))
    if plan.domain == "policy":
        if not evidence or not all(item.get("page_number") for item in evidence):
            issues.append(_issue("policy_provenance_missing", "制度答案必须带检索证据和页码"))
    if plan.domain == "script":
        if published_scripts and not any(
            str(script).strip() and str(script).strip() in content for script in published_scripts
        ):
            issues.append(_issue("script_not_published", "话术答案不在已发布话术中"))
    return AnswerValidation(not issues, tuple(issues), tuple(checked))


def compare_shadow_plans(
    case_id: str,
    old: QueryPlan,
    new: QueryPlan,
    *,
    old_answer: str = "",
    new_answer: str = "",
    old_permission: object = None,
    new_permission: object = None,
    latency_ms: Mapping[str, float] | None = None,
) -> ShadowComparison:
    issues: list[str] = []
    shape_match = old.shape == new.shape
    subjects_match = [item.to_dict() for item in old.subjects] == [
        item.to_dict() for item in new.subjects
    ]
    fields_match = [item.to_dict() for item in old.fields] == [
        item.to_dict() for item in new.fields
    ]
    filters_match = [item.to_dict() for item in old.filters] == [
        item.to_dict() for item in new.filters
    ]
    answer_match = old_answer == new_answer if old_answer or new_answer else True
    permissions_match = old_permission == new_permission
    if not shape_match:
        issues.append("shape")
    if not subjects_match:
        issues.append("subjects")
    if not fields_match:
        issues.append("fields")
    if not filters_match:
        issues.append("filters")
    if not permissions_match:
        issues.append("permissions")
    if not answer_match:
        issues.append("answer")
    return ShadowComparison(
        case_id,
        shape_match,
        subjects_match,
        fields_match,
        filters_match,
        permissions_match,
        answer_match,
        latency_ms or {},
        tuple(issues),
    )


def capabilities_snapshot() -> dict[str, object]:
    """Expose a reviewable registry snapshot; values are not model instructions."""
    return {
        "domains": sorted(DOMAINS),
        "shapes": sorted(SHAPES),
        "domain_capabilities": {
            key: {
                "shapes": sorted(value["shapes"]),
                "delivery": sorted(value["delivery"]),
                "requires_evidence": bool(value["requires_evidence"]),
            }
            for key, value in DOMAIN_CAPABILITY_REGISTRY.items()
        },
        "operations": {
            key: {
                "description": value.description,
                "max_page_size": value.max_page_size,
                "max_items": value.max_items,
                "aggregate_kinds": sorted(value.aggregate_kinds),
            }
            for key, value in CAPABILITY_REGISTRY.items()
        },
        "fields": {
            key: {
                "label": value.label,
                "value_type": value.value_type,
                "role": value.role,
                "sensitive": value.sensitive,
                "filterable": value.filterable,
                "sortable": value.can_sort,
            }
            for key, value in PRODUCT_FIELD_CATALOG.items()
        },
    }


# Explicit aliases for callers that want registry objects in protocol docs.
SYSTEM_CAPABILITY_REGISTRY = DOMAIN_CAPABILITY_REGISTRY
CAPABILITY_REGISTRY_V2 = DOMAIN_CAPABILITY_REGISTRY
