"""Regression coverage for deterministic product-condition pre-parsing."""

import json

from app.business.intent import Intent, IntentDecision
from app.business.planner import parse_task_plan, plan_from_decision
from app.business.product_query import (
    ProductQuery,
    preparse_product_query,
    validate_product_query,
)


def test_preparse_keeps_model_price_range_and_entity_count() -> None:
    message = "LY0653GDT-T价格从100到200之间有多少种产品"
    parsed = preparse_product_query(message)

    assert parsed.operation == "count"
    assert [item.value for item in parsed.references] == ["ly0653gdt-t"]
    assert [(item.field, item.operator, item.value) for item in parsed.filters] == [
        ("set_price", "between", (100.0, 200.0))
    ]

    fallback = plan_from_decision(
        IntentDecision(Intent.PRODUCT, 0.99, "规则兜底", message), message
    ).tasks[0].product_query
    assert fallback is not None
    assert fallback.operation == "count"
    assert len(fallback.filters) == 1


def test_preparse_supports_multiple_text_conditions_with_and_semantics() -> None:
    parsed = preparse_product_query("LY0653系列颜色为红色、出货地广东的产品有哪些")
    assert parsed.operation == "list"
    assert [item.value for item in parsed.references] == ["ly0653"]
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("color", "红色"),
        ("shipping_origin", "广东"),
    ]

    parsed = preparse_product_query("材质为全铜且支持恒温，档位四档的有哪些")
    assert parsed.operation == "list"
    assert {(item.field, item.value) for item in parsed.filters} == {
        ("material", "全铜"),
        ("special_function", "恒温"),
        ("spray_modes", "四档"),
    }


def test_model_plan_missing_explicit_filter_is_rejected_before_execution() -> None:
    message = "LY0653GDT-T价格从100到200之间有多少种产品"
    plan = parse_task_plan(
        json.dumps(
            {
                "confidence": 0.99,
                "tasks": [
                    {
                        "intent": "product_query",
                        "query_for_search": message,
                        "confidence": 0.99,
                        "product_query": {
                            "operation": "count",
                            "references": [
                                {
                                    "value": "LY0653GDT-T",
                                    "kind": "exact",
                                    "evidence": "LY0653GDT-T",
                                }
                            ],
                            "filters": [],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        message,
    )
    query = plan.tasks[0].product_query
    assert query is not None
    result = validate_product_query(query, message, can_view_cost=False)
    assert any("筛选条件未被保留" in error for error in result.errors)


def test_wrong_operation_is_rejected_even_when_references_are_grounded() -> None:
    message = "LY0653GDT-T价格从100到200之间有多少种产品"
    parsed = preparse_product_query(message)
    result = validate_product_query(
        ProductQuery(
            operation="list",
            references=parsed.references,
            filters=parsed.filters,
            required_references=parsed.required_references,
            required_filters=parsed.required_filters,
            required_operation=parsed.required_operation,
        ),
        message,
        can_view_cost=False,
    )
    assert any("操作与原话不一致" in error for error in result.errors)


def test_rule_fallback_keeps_sort_page_and_price_scope() -> None:
    message = "颜色为红色，按主体价从高到低列出第2页，每页3款"
    parsed = preparse_product_query(message)

    assert [(item.field, item.direction) for item in parsed.sorts] == [
        ("body_price", "desc")
    ]
    assert (parsed.page, parsed.page_size) == (2, 3)

    fallback = plan_from_decision(
        IntentDecision(Intent.PRODUCT, 0.99, "规则兜底", message), message
    ).tasks[0].product_query
    assert fallback is not None
    assert fallback.sorts == parsed.sorts
    assert (fallback.page, fallback.page_size) == (2, 3)


def test_ambiguous_price_scope_requires_clarification() -> None:
    result = validate_product_query(ProductQuery(), "价格是成本价还是售价", can_view_cost=False)
    assert result.ambiguities
    assert result.ambiguities[0].code == "price_scope"

