"""Conversation, evidence and shadow safety regressions; no live model required."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

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
    store = Catalog()
    agent = BusinessAgent(store)
    first = answer(agent, MODEL + "主体有哪些材料组成?", plan(evidence="组成"))
    assert first.content == "材质：ABS外壳铜水路"
    second = answer(agent, MODEL + "套装材料?", plan(evidence="套装材料"))
    assert "你想了解产品主体的材质，还是套装包含的配件？" in second.content
    assert len(store.calls) == 1
    third = answer(agent, choice, ProductQuery(context_mode="continuation"))
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


@pytest.mark.parametrize(
    "left,right",
    [
        ("材质：ABS", "配件：顶喷"),
        ("材质：ABS", "材质：ABS\n颜色：白色"),
        ("材质：ABS", "材质：全铜"),
    ],
)
def test_same_product_different_answer_is_visible(left, right):
    sources = [{"source_type": "product", "source_id": "same"}]
    diff = BusinessAgent._shadow_diff(
        RoutedReply(Intent.PRODUCT, left, sources),
        RoutedReply(Intent.PRODUCT, right, sources),
    )
    assert diff["same_product_ids"]
    assert not diff["same_content"]
    assert not diff["same_semantics"]


@pytest.mark.parametrize(
    "left,right",
    [
        ("请确认主体材质还是配件", "无权限"),
        ("无权限", "当前没有符合条件的已发布产品。"),
        ("请提供型号", "产品资料查询暂时不可用，请稍后重试。"),
    ],
)
def test_empty_source_lists_do_not_hide_result_type_differences(left, right):
    diff = BusinessAgent._shadow_diff(
        RoutedReply(Intent.PRODUCT, left, []),
        RoutedReply(Intent.PRODUCT, right, []),
    )
    assert diff["same_product_ids"]
    assert not diff["same_result_type"]


def test_diff_compares_requested_fields_even_when_answer_is_identical():
    reply = RoutedReply(Intent.PRODUCT, "材质：ABS", [])
    left = replace(
        reply, execution_trace={"outcome": product_outcome(reply, requested_fields=("material",))}
    )
    right = replace(
        reply, execution_trace={"outcome": product_outcome(reply, requested_fields=("components",))}
    )
    assert not BusinessAgent._shadow_diff(left, right)["same_requested_fields"]


def test_diff_redacts_costs_and_does_not_copy_source_documents():
    reply = RoutedReply(
        Intent.PRODUCT,
        "成本价：66元\n顶喷｜采购价：12元\n配件采购总价：123元",
        [
            {"source_type": "product", "source_id": "1", "content": "secret-source"},
        ],
    )
    diff = BusinessAgent._shadow_diff(reply, reply)
    summary = diff["legacy_outcome"]["content_summary"]
    assert "66" not in summary and "12" not in summary and "123" not in summary
    assert "secret-source" not in str(diff)


def test_shadow_never_delivers_v2_clarification_or_writes_formal_context(monkeypatch):
    store = Catalog()
    agent = BusinessAgent(store)
    monkeypatch.setattr(
        agent, "_business_settings", lambda: SimpleNamespace(product_query_v2_mode="shadow")
    )
    legacy = RoutedReply(
        Intent.PRODUCT,
        "材质：ABS外壳铜水路",
        [
            {"source_type": "product", "source_id": "product-1"},
        ],
    )
    monkeypatch.setattr(agent, "_answer_product_legacy", lambda *args, **kwargs: legacy)
    agent._last_product_by_user["employee-1"] = "legacy-selection"
    decision = IntentDecision(Intent.PRODUCT, 0.99, "", "", source="planner_model")
    first = agent._answer_product(
        "employee-1", MODEL + "主体有哪些材料组成?", "", decision, plan(evidence="组成")
    )
    assert first.execution_trace["shadow_diff"]["same_semantics"]
    second = agent._answer_product(
        "employee-1", MODEL + "套装材料?", "", decision, plan(evidence="套装材料")
    )
    assert second.content == legacy.content
    assert second.execution_trace["shadow_diff"]["v2_outcome"]["result_type"] == "clarification"
    assert not agent._state_get(agent._product_query_context, "employee-1")
    assert not agent._state_get(agent._product_shadow_query_context, "employee-1")
    assert agent._last_product_by_user["employee-1"] == "legacy-selection"
    assert len(store.calls) == 1


def test_real_pending_choice_routes_synonyms_only_in_on_mode(monkeypatch):
    agent = BusinessAgent(Catalog())
    answer(agent, MODEL + "套装材料", plan(evidence="套装材料"))
    fallback = IntentDecision(Intent.CONVERSATION, 0.5, "", "后者")
    monkeypatch.setattr(
        agent, "_business_settings", lambda: SimpleNamespace(product_query_v2_mode="on")
    )
    assert (
        _product_context_decision(agent, "employee-1", "后者", fallback).source
        == "product_query_context"
    )
    monkeypatch.setattr(
        agent, "_business_settings", lambda: SimpleNamespace(product_query_v2_mode="shadow")
    )
    result = _product_context_decision(agent, "employee-1", "后者", fallback)
    assert result is None or result.source != "product_query_context"


def test_unrecognized_question_with_empty_plan_cannot_become_overview():
    store = Catalog()
    reply = answer(BusinessAgent(store), MODEL + "耐不耐用?", replace(plan(), requested_fields=()))
    assert not store.calls
    assert reply.execution_trace["outcome"]["reason"] == "unspecified_fields"


def test_sensitive_differences_are_detected_without_storing_sensitive_values():
    sources = [{"source_type": "product", "source_id": "1"}]
    diff = BusinessAgent._shadow_diff(
        RoutedReply(Intent.PRODUCT, "成本价：66元", sources),
        RoutedReply(Intent.PRODUCT, "成本价：77元", sources),
    )
    assert not diff["same_content"]
    assert "66" not in diff["legacy_outcome"]["content_summary"]
    assert "77" not in diff["v2_outcome"]["content_summary"]


def test_multi_product_values_cannot_be_swapped_by_summary_normalization():
    sources = [{"source_type": "product", "source_id": value} for value in ("A", "B")]
    diff = BusinessAgent._shadow_diff(
        RoutedReply(Intent.PRODUCT, "型号：A\n材质：ABS\n型号：B\n材质：铜", sources),
        RoutedReply(Intent.PRODUCT, "型号：A\n材质：铜\n型号：B\n材质：ABS", sources),
    )
    assert not diff["same_semantics"]


def test_legacy_permission_denial_is_not_a_fact_even_with_product_sources():
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
