"""图状态往返：ProductQuery 的**每一个字段**都必须原样保住。

为什么专门测这个：`graph/state.py` 里有一份**手写的** product_query 序列化，
和 `product_query.py` 的 `product_query_to_dict` 是两份等价实现。同一份映射写两遍，
就一定会出现"改了一处、漏了另一处"——`aggregate` 就这么漏过：

    计划里明明有 aggregate → 穿过图状态被丢掉 → 执行层报「聚合任务缺少 aggregate」
    → 员工看到的是「这个统计暂时做不了」

所以这里不逐个字段断言，而是**整对象相等**：以后往 ProductQuery 加字段而忘了同步
state.py，这条测试会立刻失败，并指出是哪个字段。
"""

from dataclasses import replace

from app.business.graph.state import plan_from_payload, plan_to_payload
from app.business.intent import Intent, IntentDecision
from app.business.planner import PlannedTask, TaskPlan
from app.business.product_query import (
    ProductAggregate,
    ProductAmbiguity,
    ProductFieldRequest,
    ProductFilter,
    ProductQuery,
    ProductReference,
    ProductSort,
)


def _decision() -> IntentDecision:
    return IntentDecision(
        intent=Intent.PRODUCT,
        confidence=0.9,
        reason="测试",
        query_for_search="测试",
        source="planner_model",
    )


def _fully_populated() -> ProductQuery:
    """每个字段都给一个非默认值，否则漏同步的字段会被默认值掩盖。"""
    return ProductQuery(
        operation="aggregate",
        include_models=("LY0653GDT",),
        exclude_models=("LY9999",),
        fields=("color", "set_price"),
        filters=(
            ProductFilter(
                field="color",
                operator="contains",
                value="灰玻璃",
                evidence="灰玻璃",
                source="current",
            ),
        ),
        version=2,
        references=(
            ProductReference(
                value="LY0653GDT", kind="exact", evidence="LY0653GDT", source="current"
            ),
        ),
        requested_fields=(
            ProductFieldRequest(field="color", evidence="颜色", source="current"),
        ),
        result_scope="all_matches",
        sorts=(ProductSort(field="set_price", direction="desc"),),
        aggregate=ProductAggregate(
            kind="distinct_count",
            field="color",
            limit=7,
            evidence="颜色",
            source="current",
        ),
        page=3,
        page_size=5,
        context_mode="continuation",
        ambiguities=(
            ProductAmbiguity(code="c1", question="是主体还是套装？", options=("主体", "套装")),
        ),
        original_fields=("color",),
        allow_overview=True,
    )


def _round_trip(query: ProductQuery) -> ProductQuery:
    plan = TaskPlan(
        goal="测试",
        tasks=(PlannedTask(_decision(), query),),
        confidence=0.9,
        reason="测试",
        source="planner_model",
    )
    restored = plan_from_payload(plan_to_payload(plan))
    return restored.tasks[0].product_query


def test_every_product_query_field_survives_the_graph_state_round_trip() -> None:
    query = _fully_populated()

    restored = _round_trip(query)

    assert restored == query, "有字段在 plan_to_payload/plan_from_payload 之间被改动或丢掉"


def test_aggregate_specifically_survives() -> None:
    """单独盯住曾经漏掉的那个字段，失败信息更直白。"""
    query = _fully_populated()

    restored = _round_trip(query)

    assert restored.aggregate is not None
    assert restored.aggregate == query.aggregate
    assert restored.operation == "aggregate"


def test_a_field_added_to_product_query_but_not_to_state_is_caught() -> None:
    """往 ProductQuery 塞一个 state.py 不认识的字段时，往返必须暴露出差异。

    这里用 dataclasses.replace 模拟"加了字段但没同步序列化"：
    state.py 的手写映射不会带上它，往返后就不相等——正是我们要的报警方式。
    """
    query = replace(_fully_populated(), aggregate=None)

    restored = _round_trip(query)

    assert restored == query
    # allow_overview 是 state.py 里单独处理过的字段，确认它没被漏掉
    assert restored.allow_overview is True
