import json
from types import SimpleNamespace

import pytest

from app.business.graph.state import plan_from_payload, plan_to_payload
from app.business.intent import Intent, IntentDecision, MediaAction, decide_intent
from app.business.planner import (
    MAX_PLAN_TASKS,
    parse_task_plan,
    plan_from_decision,
    plan_tasks,
)


class FakeCompletions:
    def __init__(self, payload: dict[str, object] | str) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        content = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def fake_client(payload: dict[str, object] | str) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(payload)))


def test_compound_request_uses_structured_model_plan() -> None:
    client = fake_client(
        {
            "goal": "查询价格并发送安装视频",
            "confidence": 0.96,
            "reason": "需要先查产品，再发送对应媒体",
            "needs_clarification": False,
            "clarification_question": "",
            "tasks": [
                {
                    "intent": "product_query",
                    "query_for_search": "查询LTA-822套装价",
                    "confidence": 0.97,
                    "reason": "查询价格",
                    "product_query": {
                        "operation": "detail",
                        "include_models": ["LTA-822"],
                        "exclude_models": [],
                        "fields": ["set_price"],
                        "filters": [],
                    },
                },
                {
                    "intent": "product_media_query",
                    "query_for_search": "发送LTA-822安装视频",
                    "confidence": 0.97,
                    "reason": "发送视频",
                    "media_action": "send",
                    "media_type": "video",
                    "media_role": "product_video",
                    "product_query": {
                        "operation": "media",
                        "include_models": ["LTA-822"],
                        "exclude_models": [],
                        "fields": ["videos"],
                        "filters": [],
                    },
                },
            ],
        }
    )

    plan = plan_tasks(
        "LTA-822多少钱，再把安装视频发我",
        client=client,
        model="deepseek-chat",
        decision_resolver=decide_intent,
    )

    assert plan.source == "planner_model"
    assert [task.decision.intent for task in plan.tasks] == [
        Intent.PRODUCT,
        Intent.PRODUCT_MEDIA,
    ]
    assert plan.tasks[1].decision.media_action == MediaAction.SEND
    assert len(client.chat.completions.calls) == 1


def test_compound_request_falls_back_to_bounded_rule_plan() -> None:
    plan = plan_tasks(
        "LTA-822多少钱，再把安装视频发我",
        client=fake_client("无法解析"),
        model="deepseek-chat",
        decision_resolver=decide_intent,
    )

    assert plan.source == "planner_rules"
    assert [task.decision.intent for task in plan.tasks] == [
        Intent.PRODUCT,
        Intent.PRODUCT_MEDIA,
    ]
    assert len(plan.tasks) <= MAX_PLAN_TASKS


def test_simple_request_keeps_single_fast_task() -> None:
    calls = []

    def resolver(query: str, **_kwargs) -> IntentDecision:
        calls.append(query)
        return IntentDecision(Intent.PRODUCT, 0.97, "明确产品查询", query)

    plan = plan_tasks("LTA-822库存多少", decision_resolver=resolver)

    assert len(plan.tasks) == 1
    assert plan.tasks[0].decision.intent == Intent.PRODUCT
    assert calls == ["LTA-822库存多少"]


def test_single_product_request_uses_structured_product_plan() -> None:
    client = fake_client(
        {
            "goal": "查询产品价格",
            "confidence": 0.98,
            "tasks": [
                {
                    "intent": "product_query",
                    "query_for_search": "LTA-822这款预算怎么样",
                    "confidence": 0.98,
                    "product_query": {
                        "operation": "detail",
                        "include_models": ["LTA-822"],
                        "exclude_models": [],
                        "fields": ["set_price"],
                        "filters": [],
                    },
                }
            ],
        }
    )

    plan = plan_tasks(
        "LTA-822这款预算怎么样",
        client=client,
        model="deepseek-chat",
        decision_resolver=lambda query, **_kwargs: IntentDecision(
            Intent.PRODUCT, 0.97, "明确产品查询", query
        ),
    )

    assert plan.source == "planner_model"
    assert plan.tasks[0].product_query.fields == ("set_price",)
    assert len(client.chat.completions.calls) == 1


def test_structured_product_plan_survives_graph_state_round_trip() -> None:
    plan = parse_task_plan(
        json.dumps(
            {
                "goal": "查询其他产品",
                "confidence": 0.98,
                "tasks": [
                    {
                        "intent": "product_query",
                        "query_for_search": "除了LTA-822还有什么",
                        "confidence": 0.98,
                        "product_query": {
                            "operation": "list",
                            "include_models": [],
                            "exclude_models": ["LTA-822"],
                            "fields": ["model"],
                            "filters": [],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "除了LTA-822还有什么",
    )

    restored = plan_from_payload(plan_to_payload(plan))

    assert restored.tasks[0].product_query == plan.tasks[0].product_query


def test_planner_cannot_replace_explicit_product_code() -> None:
    plan = parse_task_plan(
        json.dumps(
            {
                "goal": "查询产品",
                "confidence": 0.9,
                "tasks": [
                    {
                        "intent": "product_query",
                        "query_for_search": "查询TTA-T01价格",
                        "confidence": 0.9,
                        "product_query": {
                            "operation": "detail",
                            "include_models": ["TTA-T01"],
                            "exclude_models": [],
                            "fields": ["set_price"],
                            "filters": [],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "LTA-822多少钱",
    )

    assert plan.tasks[0].decision.query_for_search == "LTA-822多少钱"
    assert plan.tasks[0].product_query.include_models == ("lta-822",)


def test_planner_cannot_expand_public_price_query_to_cost_price() -> None:
    plan = parse_task_plan(
        json.dumps(
            {
                "goal": "查询系列价格",
                "confidence": 0.98,
                "tasks": [
                    {
                        "intent": "product_query",
                        "query_for_search": "0653价格多少",
                        "confidence": 0.98,
                        "product_query": {
                            "operation": "detail",
                            "include_models": ["0653"],
                            "exclude_models": [],
                            "fields": ["set_price", "body_price", "cost_price"],
                            "filters": [],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "0653价格多少",
    )

    product_query = plan.tasks[0].product_query
    assert product_query is not None
    assert product_query.fields == ("set_price", "body_price")


def test_planner_rejects_unbounded_task_count() -> None:
    tasks = [
        {"intent": "product_query", "query_for_search": f"产品任务{index}"}
        for index in range(MAX_PLAN_TASKS + 1)
    ]
    with pytest.raises(ValueError, match="超过限制"):
        parse_task_plan(json.dumps({"goal": "过多任务", "tasks": tasks}), "查询产品")


def test_special_function_catalog_fallback_uses_present_filter() -> None:
    query = "哪些产品含有特殊功能"
    decision = IntentDecision(Intent.PRODUCT, 0.99, "产品目录筛选", query)

    plan = plan_from_decision(decision, query)
    product_query = plan.tasks[0].product_query

    assert product_query is not None
    assert product_query.operation == "list"
    assert product_query.fields == ("model", "special_function")
    assert len(product_query.filters) == 1
    assert product_query.filters[0].field == "special_function"
    assert product_query.filters[0].operator == "is_present"
    assert product_query.filters[0].value == ""


def test_catalog_query_preserves_product_shorthand() -> None:
    query = "0653有哪些产品"
    decision = IntentDecision(Intent.PRODUCT, 0.99, "查询系列产品", query)

    plan = plan_from_decision(decision, query)
    product_query = plan.tasks[0].product_query

    assert product_query is not None
    assert product_query.operation == "list"
    assert product_query.include_models == ("0653",)


def test_planner_accepts_is_present_filter_without_value() -> None:
    query = "哪些产品含有特殊功能"
    plan = parse_task_plan(
        json.dumps(
            {
                "goal": query,
                "confidence": 0.98,
                "tasks": [
                    {
                        "intent": "product_query",
                        "query_for_search": query,
                        "confidence": 0.98,
                        "product_query": {
                            "operation": "list",
                            "include_models": [],
                            "exclude_models": [],
                            "fields": ["model", "special_function"],
                            "filters": [
                                {
                                    "field": "special_function",
                                    "operator": "is_present",
                                    "value": "",
                                }
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        query,
    )

    product_query = plan.tasks[0].product_query
    assert product_query is not None
    assert product_query.filters[0].operator == "is_present"
    assert product_query.filters[0].value == ""
