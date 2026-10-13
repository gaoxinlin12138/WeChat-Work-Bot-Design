"""Deterministic contract coverage for the employee-message surface.

This suite deliberately stops before PostgreSQL, Redis, WeCom, or an LLM.  It
is the repeatable gate for routing, product-query semantics, reference safety,
permissions, media fan-out, and audit serialization.  Service-dependent tests
remain opt-in in the existing files.
"""

import json
from pathlib import Path

import pytest

from app.business.audit import text_fingerprint
from app.business.canonical import canonicalize_request
from app.business.intent import (
    Intent,
    IntentDecision,
    decide_intent,
    media_requests,
)
from app.business.planner import plan_from_decision, plan_tasks
from app.business.product_focus import ProductFocus
from app.business.query_governance import PermissionContext, validate_capability

MATRIX = Path(__file__).resolve().parents[1] / "evals" / "full_scenario_matrix.jsonl"
CASES = [
    json.loads(line)
    for line in MATRIX.read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_message_matrix_routes_without_external_services(case: dict[str, object]) -> None:
    decision = decide_intent(str(case["query"]), client=None, model=None)

    assert str(decision.intent) == case["expected_intent"]
    if case.get("should_clarify"):
        assert decision.needs_clarification is True
        assert decision.clarification_question
    else:
        assert decision.needs_clarification is False


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.get("operation")],
    ids=lambda case: case["case_id"],
)
def test_product_operation_contracts_preserve_user_constraints(case: dict[str, object]) -> None:
    decision = IntentDecision(Intent.PRODUCT, 0.99, "deterministic matrix", str(case["query"]))
    task = plan_from_decision(decision, str(case["query"])).tasks[0]
    assert task.product_query is not None
    assert task.product_query.operation == case["operation"]
    if case.get("field"):
        assert case["field"] in task.product_query.fields
    if case.get("aggregate_field"):
        assert task.product_query.aggregate is not None
        assert task.product_query.aggregate.field == case["aggregate_field"]
        assert task.product_query.aggregate.kind == "distinct_count"


def test_compound_media_contract_fans_out_without_dropping_a_request() -> None:
    assert media_requests("把LTA-822图片和视频都发我") == (
        ("image", None),
        ("video", "product_video"),
    )


def test_pronoun_and_ordinal_resolution_is_context_bound_and_auditable() -> None:
    focus = ProductFocus(
        result_ids=("p1", "p2"),
        result_models=("LTA-822", "LY0689"),
        trusted_ids=("p1", "p2"),
        total=2,
        expires_at=10**12,
    )
    decision = IntentDecision(Intent.PRODUCT, 0.9, "continuation", "它的材质")

    resolved = canonicalize_request(
        "它的材质",
        plan_from_decision(decision, "它的材质"),
        focus=focus,
        request_id="req-pronoun",
    )
    assert resolved.needs_clarification is True
    assert resolved.unresolved == ("它",)
    assert resolved.tasks[0].product_query is not None
    assert resolved.tasks[0].product_query.include_models == ()

    ordinal = canonicalize_request(
        "第2个的材质",
        plan_from_decision(decision, "第2个的材质"),
        focus=focus,
        request_id="req-ordinal",
    )
    assert ordinal.needs_clarification is False
    assert ordinal.unresolved == ()
    assert ordinal.tasks[0].resolution.product_ids == ("p2",)

    payload = resolved.to_dict()
    assert payload["fingerprint"] == resolved.fingerprint
    assert payload["tasks"][0]["resolution"]["unresolved"] == ["它"]


def test_single_candidate_pronoun_resolves_to_trusted_product() -> None:
    focus = ProductFocus(
        result_ids=("p1",),
        result_models=("LTA-822",),
        trusted_ids=("p1",),
        total=1,
        expires_at=10**12,
    )
    decision = IntentDecision(Intent.PRODUCT, 0.9, "continuation", "它的材质")
    canonical = canonicalize_request(
        "它的材质", plan_from_decision(decision, "它的材质"), focus=focus
    )
    assert canonical.needs_clarification is False
    assert canonical.tasks[0].resolution.product_ids == ("p1",)
    assert canonical.tasks[0].product_query is not None
    assert canonical.tasks[0].product_query.include_models == ("LTA-822",)


def test_cross_domain_request_is_bounded_to_two_auditable_tasks() -> None:
    plan = plan_tasks(
        "LTA-822价格和年假规定",
        client=None,
        model=None,
        decision_resolver=decide_intent,
    )
    assert [str(task.decision.intent) for task in plan.tasks] == [
        "product_query",
        "policy_query",
    ]
    assert len(plan.tasks) <= 4


def test_sensitive_field_requires_identity_permission_even_when_requested_explicitly() -> None:
    denied = validate_capability(
        operation="detail", fields=("cost_price",), can_view_sensitive=False
    )
    assert any(issue.code == "permission_denied" for issue in denied)
    allowed = validate_capability(
        operation="detail",
        fields=("cost_price",),
        can_view_sensitive=PermissionContext("employee-1", frozenset({"product.cost.read"})),
    )
    assert not any(issue.code == "permission_denied" for issue in allowed)


def test_pagination_cursor_is_not_reused_across_user_or_query() -> None:
    from app.business.query_governance import CursorCodec

    codec = CursorCodec(secret="matrix-secret", ttl_seconds=60)
    token = codec.encode(user_id="u1", query_fingerprint="q1", page=2)
    assert codec.decode(token, user_id="u1", query_fingerprint="q1") == 2
    assert codec.decode(token, user_id="u2", query_fingerprint="q1") is None
    assert codec.decode(token, user_id="u1", query_fingerprint="q2") is None


def test_duplicate_request_fingerprint_is_stable_but_original_is_reviewable() -> None:
    raw = "客户电话 13812345678，重复查询 LTA-822 价格"
    assert text_fingerprint(raw) == text_fingerprint(raw)
    assert text_fingerprint(raw) != raw
