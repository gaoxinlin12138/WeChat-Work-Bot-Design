"""Offline/ shadow helpers for the unified QueryPlan rollout.

The runner is intentionally store-free.  It measures planning parity and
validation decisions from replay data; it never sends a WeCom message or
mutates business data.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from app.business.intent import classify_intent
from app.business.product_query import (
    ProductAggregate,
    fallback_field_requests,
    is_overview_request,
    preparse_product_query,
)
from app.business.query_plan import (
    PlanField,
    PlanSort,
    PlanSubject,
    PlanValidation,
    QueryPlan,
    ShadowComparison,
    compare_shadow_plans,
    parse_query_plan,
    query_plan_from_product_query,
)


def build_rule_shadow_plan(message: str) -> QueryPlan:
    """Build a candidate solely from existing deterministic parsers."""
    intent = classify_intent(message)
    if intent.value == "policy_query":
        return QueryPlan(domain="policy", shape="policy", delivery="text")
    if intent.value == "reply_script_query":
        return QueryPlan(domain="script", shape="script", delivery="text")
    query = preparse_product_query(message)
    if re.search(r"(?:最贵|最高价|价格最高|贵到便宜|前三|前\s*三)", message):
        return QueryPlan(
            domain="product",
            shape="rank",
            fields=(PlanField("set_price", "最贵" if "最贵" in message else "价格", "current"),),
            sort=(PlanSort("set_price", "desc"),),
            page=1,
            page_size=3 if re.search(r"前\s*三|前三", message) else 1,
        )
    # The legacy pre-parser intentionally keeps count as a broad fallback.  In
    # the unified shadow representation, explicit distinct-dimension wording
    # has a first-class aggregate shape and never becomes an entity count.
    distinct = re.search(r"(?:多少种|几种)(颜色|材质|档位|功能|出货地|供应商)", message)
    if distinct:
        field = {
            "颜色": "color",
            "材质": "material",
            "档位": "spray_modes",
            "功能": "special_function",
            "出货地": "shipping_origin",
            "供应商": "supplier",
        }[distinct.group(1)]
        query = query.__class__(
            **{
                **query.__dict__,
                "operation": "aggregate",
                "aggregate": ProductAggregate(
                    "distinct_count", field, evidence=distinct.group(1), source="fallback"
                ),
            }
        )
    if (
        query.operation == "detail"
        and query.references
        and is_overview_request(message, query.references)
    ):
        query = replace(query, allow_overview=True)
    if query.operation == "count" and query.aggregate is None:
        # Entity count must never inherit a value-looking filter such as
        # special_function=“几种产品” from the broad legacy pre-parser.
        query = replace(
            query,
            filters=tuple(
                item
                for item in query.filters
                if not (
                    item.field == "special_function"
                    and isinstance(item.value, str)
                    and "产品" in item.value
                )
            ),
            requested_fields=(),
            fields=(),
        )
    else:
        # The legacy pre-parser records deterministic filters but leaves field
        # projection to the planner.  For the unified shadow plan, retain the
        # explicit field mentions so old/new projections are comparable.
        requests = tuple(
            dict.fromkeys((*query.effective_field_requests(), *fallback_field_requests(message)))
        )
        query = replace(
            query, requested_fields=requests, fields=tuple(item.field for item in requests)
        )
    if query.aggregate is not None:
        evidence = query.aggregate.evidence
        for phrase in ("多少钱", "价格", "价钱", "售价", "套装价", "主体价"):
            if phrase in message:
                evidence = phrase
                break
        query = replace(query, aggregate=replace(query.aggregate, evidence=evidence))
    if intent.value == "product_media_query" or query.operation == "media":
        fields = []
        if "图片" in message or "照片" in message:
            fields.append({"name": "images", "evidence": "图片" if "图片" in message else "照片"})
        if "视频" in message:
            fields.append({"name": "videos", "evidence": "视频"})
        return QueryPlan(
            domain="media",
            shape="media",
            subjects=tuple(
                PlanSubject(item.value, "model", item.evidence, "current")
                for item in query.effective_references()
            ),
            fields=tuple(
                PlanField(str(item["name"]), str(item["evidence"]), "current") for item in fields
            ),
            delivery="media",
        )
    return query_plan_from_product_query(query)


def validate_shadow_message(
    message: str, *, value_catalog: Mapping[str, Sequence[str]] | None = None
) -> PlanValidation:
    candidate = build_rule_shadow_plan(message)
    return parse_query_plan(candidate.to_dict(), message, value_catalog=value_catalog)


def replay_cases(
    cases: Sequence[Mapping[str, object]],
    *,
    old_builder: Callable[[str], QueryPlan] = build_rule_shadow_plan,
    new_builder: Callable[[str], QueryPlan] = build_rule_shadow_plan,
) -> list[ShadowComparison]:
    results: list[ShadowComparison] = []
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or case.get("case_id") or f"case-{index + 1}")
        message = str(case.get("query") or "")
        started = time.perf_counter()
        old = old_builder(message)
        old_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        new = new_builder(message)
        new_ms = (time.perf_counter() - started) * 1000
        comparison = compare_shadow_plans(
            case_id, old, new, latency_ms={"old": old_ms, "new": new_ms}
        )
        expected = case.get("expected")
        if isinstance(expected, Mapping):
            expected_shape = expected.get("shape")
            if expected_shape and new.shape != expected_shape:
                comparison = replace(
                    comparison,
                    shape_match=False,
                    issues=(*comparison.issues, "expected_shape"),
                )
        results.append(comparison)
    return results


def load_jsonl(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def replay_file(path: str | Path) -> dict[str, object]:
    results = replay_cases(load_jsonl(path))
    passed = sum(item.passed for item in results)
    return {
        "mode": "offline_shadow",
        "read_only": True,
        "sent_messages": 0,
        "mutated_records": 0,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": [item.to_dict() for item in results],
    }
