import json

import pytest

from app.business.planner import parse_task_plan
from app.business.query_governance import (
    CursorCodec,
    OutputPolicy,
    QueryResult,
    ResponseEnvelope,
    render_envelope,
    validate_capability,
)


def test_count_is_entity_operation_without_dimension_requirement() -> None:
    issues = validate_capability(operation="count", fields=(), page_size=1)
    assert issues == ()


def test_aggregate_requires_dimension_or_measure_role() -> None:
    issues = validate_capability(
        operation="aggregate", fields=("description",), aggregate_kind="distinct_count"
    )
    assert any(item.code == "field_not_aggregatable" for item in issues)


def test_model_sql_execution_payload_is_rejected() -> None:
    payload = {
        "tasks": [{"intent": "product_query", "product_query": {"sql": "select * from products"}}]
    }
    with pytest.raises(ValueError):
        parse_task_plan(json.dumps(payload), "有多少产品")


def test_cursor_is_bound_to_user_and_query_and_expires() -> None:
    codec = CursorCodec(secret="test-secret", ttl_seconds=60)
    token = codec.encode(user_id="u1", query_fingerprint="q1", page=2)
    assert codec.decode(token, user_id="u1", query_fingerprint="q1") == 2
    assert codec.decode(token, user_id="u2", query_fingerprint="q1") is None
    assert codec.decode(token, user_id="u1", query_fingerprint="q2") is None


def test_envelope_contains_pagination_and_hard_output_budget() -> None:
    result = QueryResult(total_count=12, page=1, page_size=5, has_more=True)
    envelope = ResponseEnvelope("r1", "fact", "x" * 100, result=result)
    bounded = render_envelope(envelope, policy=OutputPolicy(max_chars=40))
    assert bounded.result is not None
    assert bounded.result.truncated is True
    assert bounded.result.has_more is True
    assert "下一页" in bounded.content

