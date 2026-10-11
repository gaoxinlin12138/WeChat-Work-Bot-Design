"""Regression coverage for product count/list routing and the structured contract."""

import json
import time
from types import SimpleNamespace

import pytest

from app.business.graph.builder import (
    _count_listing_follow_up_plan,
    _ordinal_detail_follow_up_plan,
)
from app.business.intent import Intent, IntentDecision
from app.business.planner import parse_task_plan, plan_from_decision
from app.business.product_query import ProductFilter, ProductQuery, product_query_to_dict
from app.business.product_store import ProductQueryResult
from app.business.service import BusinessAgent


class _Catalog:
    def __init__(self) -> None:
        self.calls: list[ProductQuery] = []
        self.items = [
            {"id": "p1", "model": "A-100", "color": "红色"},
            {"id": "p2", "model": "B-200", "color": "蓝色"},
        ]

    def query_products_v2(self, query: ProductQuery, *, include_sensitive: bool):
        self.calls.append(query)
        items = self.items
        for item in query.filters:
            items = [row for row in items if row.get(item.field) == item.value]
        return ProductQueryResult(items[: query.page_size], len(items), 1, query.page_size)


def _answer(store: _Catalog, query: str, plan: ProductQuery):
    return BusinessAgent(store)._answer_product_v2(
        "employee-count",
        query,
        plan,
        can_view_cost=False,
        cost_access_message="无成本权限",
    )


def test_count_request_uses_product_count_with_empty_filters() -> None:
    message = "我们现在有多少种产品"
    decision = IntentDecision(Intent.PRODUCT, 0.99, "产品数量统计", message)
    planned = plan_from_decision(decision, message).tasks[0].product_query

    assert planned is not None
    assert planned.operation == "count"
    assert planned.filters == ()

    store = _Catalog()
    reply = _answer(store, message, planned)

    assert "当前共有 2 种产品。" in reply.content
    assert "A-100" in reply.content and "B-200" in reply.content
    assert store.calls[0].operation == "count"
    assert store.calls[0].page_size == 5
    assert store.calls[0].filters == ()
    assert reply.execution_trace["execution"]["total"] == 2


def test_count_request_with_inline_listing_also_shows_models() -> None:
    message = "现在有多少种产品，分别是什么"
    decision = IntentDecision(Intent.PRODUCT, 0.99, "产品数量统计", message)
    planned = plan_from_decision(decision, message).tasks[0].product_query

    assert planned is not None and planned.operation == "count"
    reply = _answer(_Catalog(), message, planned)

    assert "当前共有 2 种产品。" in reply.content
    assert "A-100" in reply.content and "B-200" in reply.content


def test_count_result_is_saved_for_a_listing_follow_up() -> None:
    store = _Catalog()
    agent = BusinessAgent(store)
    reply = agent._answer_product_v2(
        "employee-count-context",
        "现在有多少种产品",
        ProductQuery(operation="count"),
        can_view_cost=False,
        cost_access_message="无成本权限",
    )

    saved = agent._state_get(agent._product_query_context, "employee-count-context")

    assert "当前共有 2 种产品。" in reply.content
    assert "A-100" in reply.content and "B-200" in reply.content
    assert saved["plan"]["operation"] == "list"
    assert saved["selected_product_models"] == ["A-100", "B-200"]
    assert saved["total"] == 2


def test_real_agent_count_also_lists_products_on_the_first_turn(monkeypatch) -> None:
    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    agent = BusinessAgent(_Catalog())

    first = agent.answer("employee-e2e", "现在有多少种产品")
    second = agent.answer("employee-e2e", "分别是哪两种产品")

    assert "当前共有 2 种产品。" in first.content
    assert "A-100" in first.content and "B-200" in first.content
    assert "A-100" in second.content and "B-200" in second.content
    assert second.execution_trace["query_plan"]["context_mode"] == "continuation"


def test_real_agent_ordinal_follow_up_uses_displayed_focus_id(monkeypatch) -> None:
    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    store = _Catalog()
    agent = BusinessAgent(store)

    agent.answer("employee-focus", "现在有多少种产品")
    listed = agent.answer("employee-focus", "分别是哪两种产品")
    reply = agent.answer("employee-focus", "有哪些产品 第二个多少钱")

    assert "这个查询条件无法安全执行" not in reply.content
    assert "B-200" in reply.content
    assert reply.execution_trace["validation"]["accepted_fields"] == ["set_price"]
    assert reply.execution_trace["query_plan"]["resolved_product_ids"] == ["p2"]
    assert reply.execution_trace["query_plan"]["focus"]["resolved_product_model"] == "B-200"
    assert store.calls[-1].resolved_product_ids == ("p2",)
    assert "A-100" in listed.content and "B-200" in listed.content


@pytest.mark.parametrize(
    "message",
    ["分别是哪两种产品", "具体是哪两个", "把它们列出来"],
)
def test_count_listing_follow_up_reuses_the_previous_query(message: str) -> None:
    previous = ProductQuery(
        operation="count",
        filters=(ProductFilter("color", "equals", "红色", "红色"),),
    )

    plan = _count_listing_follow_up_plan(message, {"plan": product_query_to_dict(previous)})

    assert plan is not None
    query = plan.tasks[0].product_query
    assert query is not None
    assert query.operation == "list"
    assert query.context_mode == "continuation"
    assert query.allow_overview is True
    assert [(item.field, item.value, item.source) for item in query.filters] == [
        ("color", "红色", "context")
    ]


def test_expired_count_context_is_not_used_for_follow_up() -> None:
    previous = ProductQuery(operation="count")

    plan = _count_listing_follow_up_plan(
        "分别是哪两种产品",
        {"plan": product_query_to_dict(previous), "expires_at": time.time() - 1},
    )

    assert plan is None


@pytest.mark.parametrize(
    ("message", "field"),
    [("第二个多少钱", "set_price"), ("第一个的材质是什么", "material")],
)
def test_list_ordinal_follow_up_resolves_to_the_displayed_model(message: str, field: str) -> None:
    plan = _ordinal_detail_follow_up_plan(
        message,
        {
            "selected_product_models": ["A-100", "B-200"],
        },
    )

    assert plan is not None
    query = plan.tasks[0].product_query
    assert query is not None
    assert query.context_mode == "continuation"
    assert query.references[0].value == ("B-200" if "第二" in message else "A-100")
    assert query.fields == (field,)


def test_ordinal_parser_supports_product_variants_and_relative_two_item_choice() -> None:
    for message, expected in (("第二款多少钱", "B-200"), ("后者多少钱", "B-200")):
        plan = _ordinal_detail_follow_up_plan(
            message,
            {
                "selected_product_ids": ["p1", "p2"],
                "selected_product_models": ["A-100", "B-200"],
            },
        )
        assert plan is not None
        query = plan.tasks[0].product_query
        assert query is not None
        assert query.references[0].value == expected
        assert query.resolved_product_ids == ("p2",)


def test_model_empty_filter_object_is_normalized_to_empty_filter_tuple() -> None:
    message = "我们现在有多少种产品"
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
                            "filters": {},
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        message,
    )

    assert plan.tasks[0].product_query is not None
    assert plan.tasks[0].product_query.operation == "count"
    assert plan.tasks[0].product_query.filters == ()


def test_model_entity_count_is_repaired_when_it_returns_aggregate() -> None:
    message = "我们现在有多少种产品"
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
                            "operation": "aggregate",
                            "aggregate": {
                                "kind": "distinct_count",
                                "field": "model",
                                "evidence": "产品",
                            },
                            "requested_fields": [
                                {"field": "model", "evidence": "产品"}
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        message,
    )

    planned = plan.tasks[0].product_query
    assert planned is not None
    assert planned.operation == "count"
    assert planned.aggregate is None
    assert planned.fields == ()
    assert planned.requested_fields == ()


def test_model_field_aggregate_remains_aggregate() -> None:
    message = "一共有多少种颜色"
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
                            "operation": "aggregate",
                            "aggregate": {
                                "kind": "distinct_count",
                                "field": "color",
                                "evidence": "颜色",
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        message,
    )

    planned = plan.tasks[0].product_query
    assert planned is not None
    assert planned.operation == "aggregate"
    assert planned.aggregate is not None
    assert planned.aggregate.field == "color"


def test_count_ignores_stray_model_requested_fields() -> None:
    message = "产品总数"
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
                            "requested_fields": [
                                {"field": "model", "evidence": "产品"}
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        message,
    )

    planned = plan.tasks[0].product_query
    assert planned is not None
    assert planned.operation == "count"
    assert planned.requested_fields == ()


def test_conditioned_product_list_keeps_filter_contract() -> None:
    store = _Catalog()
    query = ProductQuery(
        operation="list",
        requested_fields=(),
        filters=(ProductFilter("color", "equals", "红色", "红色"),),
    )

    reply = _answer(store, "列出红色产品", query)

    assert reply.execution_trace["query_plan"]["operation"] == "list"
    assert store.calls[0].filters[0].field == "color"
    assert "A-100" in reply.content


def test_unconditioned_ordinary_list_requests_scope_clarification() -> None:
    store = _Catalog()
    reply = _answer(store, "列出产品", ProductQuery(operation="list"))

    assert "指定产品条件" in reply.content
    assert store.calls == []
    assert reply.execution_trace["outcome"]["result_type"] == "clarification"


def test_count_and_filter_combination_reaches_product_tool() -> None:
    store = _Catalog()
    query = ProductQuery(
        operation="count",
        filters=(ProductFilter("color", "equals", "红色", "红色"),),
    )

    reply = _answer(store, "有多少种红色产品", query)

    assert "当前共有 1 种产品。" in reply.content
    assert "A-100" in reply.content
    assert store.calls[0].operation == "count"
    assert [(item.field, item.value) for item in store.calls[0].filters] == [("color", "红色")]
    assert reply.execution_trace["execution_conditions"]["filters"][0]["field"] == "color"
