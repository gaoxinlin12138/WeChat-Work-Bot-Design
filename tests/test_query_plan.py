import json

from app.business.graph.builder import _apply_unified_query_plan
from app.business.intent import Intent, IntentDecision
from app.business.planner import PlannedTask, TaskPlan
from app.business.product_query import ProductQuery, ProductReference
from app.business.query_governance import PermissionContext
from app.business.query_plan import (
    PlanField,
    PlanSubject,
    QueryPlan,
    compare_shadow_plans,
    parse_query_plan,
    review_query_plan,
    validate_answer,
)


def payload(**overrides):
    value = {
        "version": 1,
        "domain": "product",
        "shape": "detail",
        "subjects": [
            {"value": "LTA-822", "kind": "model", "evidence": "LTA-822", "source": "current"}
        ],
        "fields": [{"name": "material", "evidence": "材质", "source": "current"}],
        "filters": [],
        "aggregate": None,
        "sort": [],
        "page": 1,
        "page_size": 1,
        "delivery": "text",
        "context_mode": "independent",
        "clarification": None,
    }
    value.update(overrides)
    return value


def test_query_plan_has_fixed_json_shape_and_validates_evidence():
    result = parse_query_plan(payload(), "LTA-822 的材质")
    assert result.passed
    assert QueryPlan.from_dict(result.plan.to_dict()).to_dict() == result.plan.to_dict()
    assert set(result.plan.to_dict()) == {
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


def test_unknown_field_and_unquoted_filter_are_rejected():
    result = parse_query_plan(
        payload(
            fields=[{"name": "made_up", "evidence": "材质"}],
            filters=[{"field": "color", "operator": "equals", "value": "蓝色", "evidence": "颜色"}],
        ),
        "LTA-822 的材质",
    )
    assert not result.passed
    assert {item.code for item in result.issues} >= {"unknown_field", "unquoted_filter_value"}


def test_sql_payload_never_reaches_plan_parser():
    result = parse_query_plan(
        json.dumps({"domain": "product", "shape": "detail", "sql": "select *"}), "查询"
    )
    assert not result.passed
    assert any(item.code == "unsafe_payload" for item in result.issues)


def test_reviewer_repairs_detail_overview_once_without_inventing_fields():
    plan = QueryPlan(
        domain="product",
        shape="detail",
        subjects=(PlanSubject("LTA-822", "model", "LTA-822"),),
    )
    review = review_query_plan(plan, "发我 LTA-822 的详细信息")
    assert review.passed
    assert review.corrected_plan is not None
    assert review.corrected_plan.shape == "detail_full"
    assert review.corrected_plan.fields == ()
    assert review.correction_attempts == 1


def test_production_adapter_uses_reviewed_plan_and_preserves_v2_metadata():
    query = ProductQuery(
        operation="detail",
        include_models=("LTA-822",),
        references=(ProductReference("LTA-822", "exact", "LTA-822", "current"),),
        resolved_product_ids=("product-1",),
    )
    task_plan = TaskPlan(
        goal="发我 LTA-822 的详细信息",
        tasks=(
            PlannedTask(
                IntentDecision(
                    intent=Intent.PRODUCT,
                    confidence=0.99,
                    reason="test",
                    query_for_search="发我 LTA-822 的详细信息",
                ),
                query,
            ),
        ),
        confidence=0.99,
        reason="test",
        source="test",
    )
    result = _apply_unified_query_plan(
        task_plan,
        message="发我 LTA-822 的详细信息",
        user_id="employee-1",
        can_view_sensitive=False,
        enabled=True,
    )
    canonical = result.tasks[0].product_query
    assert canonical is not None
    assert canonical.allow_overview is True
    assert canonical.resolved_product_ids == ("product-1",)


def test_detail_unknown_field_is_clarification_not_full_record():
    plan = QueryPlan(
        domain="product",
        shape="detail",
        subjects=(PlanSubject("LTA-822", "model", "LTA-822"),),
    )
    result = parse_query_plan(plan.to_dict(), "LTA-822 的量子涂层")
    assert not result.passed
    assert any(item.code == "missing_fields" for item in result.issues)


def test_media_plan_requires_media_delivery_and_does_not_become_detail():
    result = parse_query_plan(
        payload(domain="media", shape="media", fields=[{"name": "images", "evidence": "图片"}]),
        "发我 LTA-822 的图片",
    )
    assert not result.passed
    assert any(item.code == "media_delivery" for item in result.issues)
    good = parse_query_plan(
        payload(
            domain="media",
            shape="media",
            delivery="media",
            fields=[{"name": "images", "evidence": "图片"}],
        ),
        "发我 LTA-822 的图片",
    )
    assert good.passed


def test_sensitive_field_is_decided_by_identity_permission():
    denied = parse_query_plan(
        payload(fields=[{"name": "cost_price", "evidence": "成本价"}]),
        "LTA-822 的成本价",
    )
    assert any(item.code == "permission_denied" for item in denied.issues)
    allowed = parse_query_plan(
        payload(fields=[{"name": "cost_price", "evidence": "成本价"}]),
        "LTA-822 的成本价",
        permission=PermissionContext("u1", frozenset({"product.cost.read"})),
    )
    assert allowed.passed


def test_answer_validation_requires_store_values_and_policy_pages():
    plan = QueryPlan(
        "product",
        "detail",
        (PlanSubject("LTA-822", evidence="LTA-822"),),
        (PlanField("material", "材质"),),
    )
    assert validate_answer(plan, "材质：ABS", records=({"material": "ABS"},)).passed
    assert not validate_answer(plan, "材质：ABS", records=()).passed
    policy = QueryPlan("policy", "policy")
    assert not validate_answer(policy, "制度内容").passed
    assert validate_answer(policy, "制度内容", evidence=({"page_number": 2},)).passed


def test_shadow_comparison_reports_plan_differences():
    old = QueryPlan("product", "listing")
    new = QueryPlan("product", "detail")
    result = compare_shadow_plans("x", old, new)
    assert not result.passed
    assert "shape" in result.issues


def test_cross_domain_registry_keeps_policy_and_script_out_of_product_executor():
    policy = parse_query_plan(
        {
            "domain": "policy",
            "shape": "policy",
            "subjects": [],
            "fields": [],
            "filters": [],
            "aggregate": None,
            "sort": [],
            "page": 1,
            "page_size": 5,
            "delivery": "text",
            "context_mode": "independent",
            "clarification": None,
        },
        "公司的年假制度",
    )
    script = parse_query_plan(
        {
            "domain": "script",
            "shape": "script",
            "subjects": [],
            "fields": [],
            "filters": [],
            "aggregate": None,
            "sort": [],
            "page": 1,
            "page_size": 5,
            "delivery": "text",
            "context_mode": "independent",
            "clarification": None,
        },
        "客户嫌贵怎么回",
    )
    assert policy.passed and script.passed
