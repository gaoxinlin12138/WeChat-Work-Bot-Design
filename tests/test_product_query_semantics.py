"""Conversation, evidence and V2 safety regressions; no live model required."""

from dataclasses import replace

import pytest

from app.business import pending
from app.business.graph.builder import _product_context_decision
from app.business.graph.state import plan_from_payload, plan_to_payload
from app.business.intent import Intent, IntentDecision
from app.business.planner import PlannedTask, TaskPlan, _ground_product_query
from app.business.product_outcome import product_outcome
from app.business.product_query import (
    ProductFieldRequest,
    ProductFilter,
    ProductQuery,
    ProductReference,
    fallback_field_requests,
    product_query_from_dict,
    product_query_to_dict,
    validate_product_query,
)
from app.business.product_store import ProductQueryResult
from app.business.service import BusinessAgent
from app.business.tools.common import RoutedReply

MODEL = "LY0653GDT-T"


class Catalog:
    def __init__(self):
        self.calls = []
        self.items = [
            {
                "id": "product-1",
                "model": MODEL,
                "material": "ABS外壳铜水路",
                "color": "白色",
                "set_price": 100,
                "cost_price": 66,
                "components": [{"name": "顶喷", "unit_price": 12}],
            }
        ]

    def query_products_v2(self, query, *, include_sensitive):
        self.calls.append((query, include_sensitive))
        return ProductQueryResult(self.items, len(self.items), query.page, query.page_size)


def plan(field="material", evidence="材质", **kwargs):
    return ProductQuery(
        references=(ProductReference(MODEL, "exact", MODEL),),
        requested_fields=(ProductFieldRequest(field, evidence),),
        **kwargs,
    )


def answer(agent, text, query, user="employee-1", **kwargs):
    return agent._answer_product_v2(
        user,
        text,
        query,
        can_view_cost=kwargs.pop("can_view_cost", False),
        cost_access_message="无权限",
        **kwargs,
    )


@pytest.mark.parametrize(
    "phrase,evidence",
    [
        ("主体有哪些材料组成?", "主体有哪些材料组成"),
        ("主体有哪些材料组成?", "组成"),
        ("主体由哪些材料组成？", "由哪些材料组成"),
        ("主体是用什么做的？", "用什么做"),
        ("主体用了什么材料？", "材料"),
        ("主体用料是什么？", "用料"),
        ("本体采用什么制作？", "采用什么制作"),
    ],
)
def test_material_semantics_accept_literal_natural_evidence(phrase, evidence):
    store = Catalog()
    reply = answer(BusinessAgent(store), MODEL + phrase, plan(evidence=evidence))
    assert reply.content == "材质：ABS外壳铜水路"
    assert store.calls[0][0].fields == ("material",)
    assert reply.execution_trace["outcome"]["result_type"] == "fact"


def test_full_current_sentence_evidence_is_valid_when_planner_splits_context():
    message = "LY0653GDT-T主体有哪些材料组成?"
    query = ProductQuery(
        references=(ProductReference(MODEL, "exact", MODEL),),
        requested_fields=(ProductFieldRequest("material", "主体有哪些材料组成?", "current"),),
    )
    result = validate_product_query(query, message, can_view_cost=False)
    assert result.query.fields == ("material",)
    assert not result.removed_fields


def test_equivalent_ascii_and_chinese_punctuation_still_ground_evidence():
    result = validate_product_query(
        ProductQuery(requested_fields=(ProductFieldRequest("material", "主体有哪些材料组成?"),)),
        "LY0653GDT-T主体有哪些材料组成？", can_view_cost=False,
    )
    assert result.query.fields == ("material",)


def test_planner_search_rewrite_cannot_replace_employee_message_for_evidence():
    message = "LY0653GDT-T主体有哪些材料组成?"
    rewritten_search = "LY0653GDT-T 材质"
    query = ProductQuery(
        references=(ProductReference(MODEL, "exact", MODEL),),
        requested_fields=(ProductFieldRequest("material", "主体有哪些材料组成?", "current"),),
    )
    # The graph now passes message as original_query and search rewrite separately.
    assert rewritten_search != message
    assert validate_product_query(query, message, can_view_cost=False).query.fields == ("material",)


@pytest.mark.parametrize(
    "field,evidence,source",
    [
        ("material", "主体材料", "current"),  # paraphrase is not a literal quotation
        ("material", "", "current"),
        ("components", "材料", "current"),
        ("cost_price", "材料", "current"),
        ("material", "伪造的依据", "fallback"),
        ("components", "材料", "fallback"),
        ("components", "材料", "context"),
    ],
)
def test_illegal_evidence_never_turns_into_full_detail(field, evidence, source):
    store = Catalog()
    query = replace(plan(), requested_fields=(ProductFieldRequest(field, evidence, source),))
    reply = answer(BusinessAgent(store), MODEL + "主体有哪些材料组成?", query, can_view_cost=True)
    assert store.calls == []
    assert reply.execution_trace["outcome"]["reason"] == "all_requested_fields_removed"
    assert "ABS" not in reply.content
    assert "100" not in reply.content


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("不是问材质，是问配件", {"components"}),
        ("不是问材质是问配件", {"components"}),
        ("不要配件，只看材质", {"material"}),
        ("套装包含哪些配件", {"components"}),
        ("套装由哪些组成", {"components"}),
        ("主体价格多少", {"body_price"}),
        ("价格多少", {"set_price"}),
    ],
)
def test_shared_semantics_respect_scope_and_negation(phrase, expected):
    assert {item.field for item in fallback_field_requests(phrase)} == expected


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("配件", "配件明细"),
        ("后者", "配件明细"),
        ("我问主体用料", "材质：ABS外壳铜水路"),
        ("前者", "材质：ABS外壳铜水路"),
    ],
)
def test_original_two_turns_then_clarification_choice(choice, expected):
    """原报告的两次对话缺陷：澄清之后员工照答，必须被接住。

    接回逻辑现在位于计划层的共享槽位（`app.business.pending`），不再是
    V2 上下文里的 `pending_ambiguity`，所以第三步先经槽位解析、再执行。
    """
    store = Catalog()
    agent = BusinessAgent(store)
    first = answer(agent, MODEL + "主体有哪些材料组成?", plan(evidence="组成"))
    assert first.content == "材质：ABS外壳铜水路"
    second = answer(agent, MODEL + "套装材料?", plan(evidence="套装材料"))
    assert "你想了解产品主体的材质，还是套装包含的配件？" in second.content
    assert len(store.calls) == 1

    resolved = pending.resolve(agent, "employee-1", choice)
    assert resolved is not None, "待澄清槽位没有接住员工的选项"
    resolved_query = resolved.tasks[0].product_query
    assert resolved_query is not None
    third = answer(agent, choice, resolved_query)
    assert expected in third.content
    assert len(store.calls) == 2
    assert not agent._product_query_context["employee-1"].get("pending_ambiguity")


def test_new_model_cannot_be_overridden_by_pending_choice():
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "套装材料", plan(evidence="套装材料"))
    query = ProductQuery(
        references=(ProductReference("OTHER-123", "exact", "OTHER-123"),),
        requested_fields=(ProductFieldRequest("components", "配件"),),
        context_mode="continuation",
    )
    answer(agent, "OTHER-123配件", query)
    assert store.calls[0][0].include_models == ("OTHER-123",)


@pytest.mark.parametrize("mode", ["independent", "continuation"])
def test_forged_context_is_rejected_even_without_continuation(mode):
    query = replace(
        plan(),
        requested_fields=(ProductFieldRequest("cost_price", "", "context"),),
        context_mode=mode,
    )
    result = validate_product_query(query, MODEL + "材质", can_view_cost=True)
    assert result.removed_fields == ("cost_price",)
    assert not result.query.fields


@pytest.mark.parametrize("reset", ["other_user", "expired", "topic_change"])
def test_context_is_not_shared_or_reused_after_expiry_or_topic_change(reset):
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "套装材料", plan(evidence="套装材料"))
    if reset == "expired":
        agent._product_query_context._memory_expiry["employee-1"] = 0
    elif reset == "topic_change":
        agent._clear_product_context("employee-1")
    reply = answer(
        agent,
        "配件",
        ProductQuery(context_mode="continuation"),
        user="employee-2" if reset == "other_user" else "employee-1",
    )
    assert not store.calls
    assert "顶喷" not in reply.content


def test_empty_fields_survive_grounding_and_both_serialization_boundaries():
    query = _ground_product_query(plan("cost_price", "价格"), MODEL + "价格")
    assert not query.fields
    assert query.original_fields == ("cost_price",)
    restored = product_query_from_dict(product_query_to_dict(query))
    task_plan = TaskPlan(
        "查询",
        (PlannedTask(IntentDecision(Intent.PRODUCT, 0.99, "", ""), restored),),
        0.99,
        "",
        "planner_model",
    )
    restored = plan_from_payload(plan_to_payload(task_plan)).tasks[0].product_query
    store = Catalog()
    reply = answer(BusinessAgent(store), MODEL + "价格", restored)
    assert not store.calls
    assert reply.execution_trace["validation"]["removed_fields"] == ["cost_price"]


def test_formatter_has_its_own_empty_field_guard():
    store = Catalog()
    reply = BusinessAgent(store)._format_product_v2(
        ProductQueryResult(store.items, 1, 1, 5),
        ProductQuery(original_fields=("material",)),
        can_view_cost=True,
    )
    assert not reply.sources
    assert "ABS" not in reply.content


def test_explicit_overview_and_partial_field_failure():
    store = Catalog()
    agent = BusinessAgent(store)
    overview = answer(agent, MODEL + "完整资料", replace(plan(), requested_fields=()))
    assert "材质：ABS" in overview.content and "套装价：100" in overview.content
    query = replace(
        plan(),
        requested_fields=(
            ProductFieldRequest("material", "材质"),
            ProductFieldRequest("components", "材质"),
        ),
    )
    reply = answer(agent, MODEL + "材质", query)
    assert "材质：ABS" in reply.content
    assert "未能可靠确认" in reply.content
    assert "顶喷" not in reply.content


def test_denied_cost_and_ordinary_price_with_cost_access():
    store = Catalog()
    agent = BusinessAgent(store)
    denied = answer(agent, MODEL + "成本价", plan("cost_price", "成本价"))
    assert not store.calls
    assert denied.execution_trace["outcome"]["result_type"] == "permission_denied"
    query = replace(
        plan("set_price", "价格"),
        requested_fields=(
            ProductFieldRequest("set_price", "价格"),
            ProductFieldRequest("cost_price", "价格"),
        ),
    )
    reply = answer(agent, MODEL + "价格多少", query, can_view_cost=True)
    assert "66" not in reply.content
    assert store.calls[0][0].fields == ("set_price",)


def test_pagination_revalidates_real_context_and_rejects_forged_filters():
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "材质", plan())
    answer(agent, "下一页", ProductQuery(context_mode="continuation"))
    assert store.calls[-1][0].page == 2
    assert store.calls[-1][0].fields == ("material",)
    query = ProductQuery(
        context_mode="continuation",
        filters=(ProductFilter("cost_price", "equals", 66, "", "context"),),
    )
    reply = answer(agent, "它", query, can_view_cost=True)
    assert len(store.calls) == 2
    assert reply.execution_trace["outcome"]["result_type"] == "validation_failed"


def test_current_field_does_not_inherit_old_material_request():
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "材质", plan())
    query = ProductQuery(
        context_mode="continuation",
        requested_fields=(
            ProductFieldRequest("material", "", "context"),
            ProductFieldRequest("color", "颜色"),
        ),
    )
    reply = answer(agent, "它颜色呢", query)
    assert store.calls[-1][0].fields == ("color",)
    assert "材质：" not in reply.content


def test_pending_choice_is_resolved_by_the_shared_slot():
    """澄清接回由计划层共享槽位处理。"""
    agent = BusinessAgent(Catalog())
    fallback = IntentDecision(Intent.CONVERSATION, 0.5, "", "后者")
    answer(agent, MODEL + "套装材料", plan(evidence="套装材料"))
    resolved = pending.resolve(agent, "employee-1", "后者")
    assert resolved is not None
    assert resolved.tasks[0].product_query.fields == ("components",)

    # 计划层的上下文判断不再自行处理待澄清，避免两套机制并存。
    decided = _product_context_decision(agent, "employee-1", "后者", fallback)
    assert decided is None or decided.source != "product_query_context"


def test_unrecognized_question_with_empty_plan_cannot_become_overview():
    store = Catalog()
    reply = answer(BusinessAgent(store), MODEL + "耐不耐用?", replace(plan(), requested_fields=()))
    assert not store.calls
    assert reply.execution_trace["outcome"]["reason"] == "unspecified_fields"


def test_permission_denial_is_not_a_fact_even_with_product_sources():
    reply = RoutedReply(
        Intent.PRODUCT,
        "你当前没有查看成本价的权限。",
        [
            {"source_type": "product", "source_id": "1"},
        ],
    )
    assert product_outcome(reply)["result_type"] == "permission_denied"


def test_new_invalid_model_question_clears_old_pending_choice():
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "套装材料", plan(evidence="套装材料"))
    query = ProductQuery(references=(ProductReference("OTHER-123", "exact", "OTHER-123"),))
    answer(agent, "OTHER-123耐不耐用", query)
    reply = answer(agent, "后者", ProductQuery(context_mode="continuation"))
    assert not store.calls
    assert "顶喷" not in reply.content


def test_listing_semantics_keep_model_and_function_filter():
    query = ProductQuery(
        operation="list",
        fields=("model", "special_function"),
        filters=(ProductFilter("special_function", "contains", "恒温", "恒温"),),
    )
    result = validate_product_query(query, "哪些产品支持恒温", can_view_cost=False)
    assert not result.errors
    assert result.query.fields == ("model", "special_function")
    assert len(result.query.filters) == 1


def test_full_overview_pagination_is_still_allowed():
    store = Catalog()
    agent = BusinessAgent(store)
    answer(agent, MODEL + "完整资料", replace(plan(), requested_fields=()))
    reply = answer(agent, "下一页", ProductQuery(context_mode="continuation"))
    assert "材质：ABS" in reply.content
    assert len(store.calls) == 2


@pytest.mark.parametrize("bad_part", ["reference", "filter", "source"])
def test_empty_or_illegal_provenance_cannot_bypass_validation(bad_part):
    store = Catalog()
    query = plan()
    if bad_part == "reference":
        query = replace(query, references=(ProductReference(MODEL, "exact", ""),))
    elif bad_part == "filter":
        query = replace(query, filters=(ProductFilter("material", "contains", "ABS", ""),))
    else:
        query = replace(query, requested_fields=(ProductFieldRequest("material", "材质", "fake"),))
    reply = answer(BusinessAgent(store), MODEL + "材质包含ABS", query)
    assert not store.calls
    assert reply.execution_trace["outcome"]["result_type"] in {"validation_failed", "clarification"}


def test_negated_set_material_does_not_trigger_ambiguity():
    result = validate_product_query(
        plan("set_price", "价格"),
        MODEL + "不要套装材料，只问价格",
        can_view_cost=False,
    )
    assert not result.ambiguities
    assert result.query.fields == ("set_price",)


@pytest.mark.parametrize(
    ("message", "field", "value"),
    [
        ("镜面玻璃·镜面 3 款是哪三款", "color", "镜面玻璃·镜面"),
        ("灰玻璃·枪灰产品有哪些", "color", "灰玻璃·枪灰"),
        ("全铜的有哪些", "material", "全铜"),
    ],
)
def test_filter_value_quoted_from_the_message_is_enough_provenance(
    message: str, field: str, value: str
):
    """筛选值被原话原样引用就是依据，不要求原话里再出现"颜色"这类字段词。

    生产上「镜面玻璃·镜面 3 款是哪三款」被拒答（"这个查询条件无法安全执行"）：
    员工承接上一轮的统计结果追问时不会重复说"颜色"，而校验只认字段语义片段。
    值本来就在原话里，把它当作依据既不放松安全性，也不会误答。
    """
    query = ProductQuery(
        operation="list",
        filters=(
            ProductFilter(field, "contains", value, value, "current"),
        ),
    )

    result = validate_product_query(query, message, can_view_cost=False)

    assert not result.errors
    assert len(result.query.filters) == 1


def test_a_value_the_employee_never_said_is_still_rejected():
    """放宽不能过头：值不在原话里，照样拦下。"""
    query = ProductQuery(
        operation="list",
        filters=(ProductFilter("color", "equals", "红色", "红色", "current"),),
    )

    result = validate_product_query(query, "镜面玻璃·镜面 3 款是哪三款", can_view_cost=False)

    assert result.errors
    assert not result.query.filters


def test_a_filter_without_any_evidence_is_still_rejected():
    """依据为空仍属残缺计划，不能靠"值恰好出现在原话里"过关。"""
    query = ProductQuery(
        operation="list",
        filters=(ProductFilter("material", "contains", "ABS", ""),),
    )

    result = validate_product_query(query, MODEL + "材质包含ABS", can_view_cost=False)

    assert result.errors
    assert not result.query.filters
