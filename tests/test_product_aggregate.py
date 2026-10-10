"""统计类提问（有多少种/最值/平均）的回归测试。

背景：员工问「现在的产品里，一共有多少种颜色」，模型理解是对的
（op=aggregate、field=color），但执行层没有聚合能力，只能退化成 61 款产品的列表
——员工要自己翻 13 页数。正确答案是 12 种。

实现上刻意**在取回结果之后用 Python 统计**，不下推成 SQL 聚合：这样权限过滤、
字段语义、引用解析都复用同一条查询路径。代价是要把匹配项都取回来，
所以这里同时盯住"分页上限必须为统计打开"——否则只统计前 5 款，答出来的数字是错的。
"""

from app.business.product_query import ProductAggregate, ProductQuery
from app.business.product_store import AGGREGATE_MAX_ROWS, ProductQueryResult
from app.business.service import BusinessAgent


class _Catalog:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items
        self.page_sizes: list[int] = []
        self.result_scopes: list[str] = []

    def search_products(self, *_args, **_kwargs):  # pragma: no cover - 本文件不触发
        return []

    def query_products_v2(self, query, *, include_sensitive):
        self.page_sizes.append(query.page_size)
        self.result_scopes.append(query.result_scope)
        size = query.page_size or 5
        return ProductQueryResult(self.items[:size], len(self.items), 1, size)


def _query(kind: str, field: str, **kwargs: object) -> ProductQuery:
    return ProductQuery(
        operation="aggregate",
        aggregate=ProductAggregate(
            kind=kind,  # type: ignore[arg-type]
            field=field,
            evidence="依据",
            source="current",
            **kwargs,  # type: ignore[arg-type]
        ),
    )


COLORS = ["灰玻璃·枪灰", "灰玻璃·枪灰", "奶白", "电镀", None]


def _color_items() -> list[dict[str, object]]:
    return [
        {"id": f"p{index}", "model": f"M{index}", "color": color}
        for index, color in enumerate(COLORS, 1)
    ]


def test_distinct_count_reports_the_number_of_values_not_products() -> None:
    """「一共有多少种颜色」要答"几种"，不是"几款"。"""
    agent = BusinessAgent(_Catalog(_color_items()))

    reply = agent._answer_product_aggregate(_query("distinct_count", "color"), can_view_cost=False)

    assert reply is not None
    # 5 款里 1 款没登记颜色 → 3 种（灰玻璃·枪灰 / 奶白 / 电镀）
    assert "3 种颜色" in reply.content
    assert "灰玻璃·枪灰 2 款" in reply.content
    # 没登记的那一款要单独说明，不能混进种类数里
    assert "1 款未登记" in reply.content


def test_aggregate_asks_the_store_for_every_match() -> None:
    """分页上限必须为统计打开。

    存储层为防刷屏把 page_size 夹到 5；统计如果也被夹到 5，就只算前 5 款，
    于是「一共有多少种颜色」答成 2 种（实际 12 种）——错的比不答更糟。
    """
    store = _Catalog(_color_items())
    agent = BusinessAgent(store)

    agent._answer_product_aggregate(_query("distinct_count", "color"), can_view_cost=False)

    assert store.page_sizes == [AGGREGATE_MAX_ROWS]


def test_aggregate_ignores_result_scope_first() -> None:
    """统计必须无视 result_scope=first。

    模型对"哪个"这类提问会给 first（听起来只要一条），而存储层对 first 会把
    page_size 压成 1——于是「最便宜的是哪个」变成"在当前这 1 条里最便宜"，
    答案直接错（生产上出现过：61 款答成"当前 1 款…234.00元"）。
    统计的口径永远是全部匹配项，与"只显示第一条"是两回事。
    """
    store = _Catalog(
        [
            {"id": "p1", "model": "A", "set_price": 348.0},
            {"id": "p2", "model": "B", "set_price": 125.0},
        ]
    )
    agent = BusinessAgent(store)

    reply = agent._answer_product_aggregate(
        _query("min", "set_price"), can_view_cost=False
    )

    assert store.result_scopes == ["all_matches"]
    assert reply is not None
    assert "125.00" in reply.content
    assert "2 款已登记" in reply.content


def test_distinct_count_ignores_the_model_supplied_limit() -> None:
    """每页列几个由代码决定，不由模型给的 limit 决定。

    模型给的 limit 若被当成"截断数量"，就会重新引入"列不完又不给翻页"的老问题。
    """
    items = _grouped_items([f"色{index:02d}" for index in range(12)])
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(
        _query("distinct_count", "color", limit=3), can_view_cost=False
    )

    assert reply is not None
    assert "第 1/2 页" in reply.content
    # limit=3 不再截断成 3 条，而是按每页 10 条给满一页
    value_lines = [line for line in reply.content.splitlines() if line.endswith("款")]
    assert len(value_lines) == 10


def test_an_identifier_field_is_refused_by_declaration_not_by_ratio() -> None:
    """标识符由**本体声明**拦下，而不是靠"取值数/行数"的事后启发式。

    series / model 在 schema.py 里声明为 identifier：数它等于数产品行数。
    这里刻意用"取值只占一半行数"的数据（比例检查不会触发），
    确认它仍然是靠**声明**被拦下的。
    """
    items = [
        {"id": f"p{i}", "model": f"M{i:04d}", "series": f"系列{i % 2}"} for i in range(10)
    ]
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(
        _query("distinct_count", "series"), can_view_cost=False
    )

    assert reply is not None
    assert "逐款登记" in reply.content
    assert "2 种系列" not in reply.content


def test_the_family_derived_dimension_turns_a_refusal_into_a_real_answer() -> None:
    """员工问"有几个系列"要的是型号族群，本体里声明了这条派生规则。

    以前只能回"按它分类得不到有意义的结果"——不撒谎，但没解决问题。
    现在顺手把族群算出来给出真答案。
    """
    items = [
        {"id": "p1", "model": "LY0594GT1", "series": "594套装A"},
        {"id": "p2", "model": "LY0594NBT", "series": "594套装B"},
        {"id": "p3", "model": "LY0621GT", "series": "621套装"},
        {"id": "p4", "model": "LY0621NBT", "series": "621套装改"},
    ]
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(_query("distinct_count", "series"), can_view_cost=False)

    assert reply is not None
    assert "2 个系列" in reply.content
    assert "LY0594（2 款）" in reply.content
    assert "LY0621（2 款）" in reply.content


def test_max_and_min_name_the_extreme_product() -> None:
    items = [
        {"id": "p1", "model": "A", "set_price": 100.0},
        {"id": "p2", "model": "B", "set_price": 348.0},
        {"id": "p3", "model": "C", "set_price": 215.0},
    ]
    agent = BusinessAgent(_Catalog(items))

    highest = agent._answer_product_aggregate(_query("max", "set_price"), can_view_cost=False)
    lowest = agent._answer_product_aggregate(_query("min", "set_price"), can_view_cost=False)

    assert highest is not None and "B" in highest.content and "348.00" in highest.content
    assert lowest is not None and "A" in lowest.content and "100.00" in lowest.content


def test_average_ignores_products_without_a_value() -> None:
    """未登记的不参与平均，否则平均值会被 NULL 拉低。"""
    items = [
        {"id": "p1", "model": "A", "set_price": 100.0},
        {"id": "p2", "model": "B", "set_price": 200.0},
        {"id": "p3", "model": "C", "set_price": None},
    ]
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(_query("average", "set_price"), can_view_cost=False)

    assert reply is not None
    assert "150.00" in reply.content
    assert "2 款已登记" in reply.content


def _grouped_items(values: list[str], *, per_value: int = 5) -> list[dict[str, object]]:
    """构造"每个取值对应多款产品"的真实形态。

    注意两点：
      - 不要用"每款一个唯一取值"的 fixture：那正是退化形态（series 就是那样），
        会被拦截，于是测不到分页/列全这些行为。
      - 聚合要用**维度字段**（color/material/notes…）。`series`、`model` 在本体里
        被声明为标识符（每行一个值），对它们做去重计数是构造上不允许的。
    """
    items: list[dict[str, object]] = []
    index = 0
    for value in values:
        for _ in range(per_value):
            items.append(
                {"id": f"p{index}", "model": f"M{index}", "series": value, "color": value}
            )
            index += 1
    return items


def test_distinct_count_pages_instead_of_hiding_values() -> None:
    """取值多于每页数量时给出页码提示，而不是"另有 N 种未列出"。

    生产上真实发生过：只有 12 种却截到 10、再补一句「另有 2 种未列出」——
    既给了总数又藏了内容，员工只能再问一遍"那两种是什么"。
    现在改成和产品清单一样的翻页：列不完就给提示，想看全就能看全。
    """
    # 零填充让字典序与数字序一致，页码切分才是可预期的；每个取值配 5 款，
    # 避免落入"每款一个取值"的退化形态（那是 series 的形态，另有守卫处理）
    items = _grouped_items([f"色{index:02d}" for index in range(12)])
    agent = BusinessAgent(_Catalog(items))

    first = agent._answer_product_aggregate(_query("distinct_count", "color"), can_view_cost=False)

    assert first is not None
    assert "12 种颜色" in first.content
    assert "第 1/2 页" in first.content
    assert '回复"下一页"继续查看。' in first.content
    assert "未列出" not in first.content
    assert "色00 5 款" in first.content
    assert "色09 5 款" in first.content
    assert "色10" not in first.content
    assert first.execution_trace["total_values"] == 12

    second = agent._answer_product_aggregate(
        _query("distinct_count", "color"), can_view_cost=False, page=2
    )

    assert second is not None
    assert "第 2/2 页" in second.content
    assert "色10 5 款" in second.content
    assert "色11 5 款" in second.content
    assert "已经是最后一页" in second.content


def test_distinct_count_does_not_page_when_one_page_is_enough() -> None:
    """一页能列完就不提翻页，免得给没有必要的操作提示。"""
    colors = [f"色{i}" for i in range(8)]
    items = _grouped_items(colors, per_value=3)
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(_query("distinct_count", "color"), can_view_cost=False)

    assert reply is not None
    assert "8 种颜色" in reply.content
    assert "页" not in reply.content
    for color in colors:
        assert color in reply.content


def test_a_field_that_is_unique_per_product_is_reported_as_not_a_category() -> None:
    """取值数≈产品数时如实说明，而不是回"61 款产品一共有 61 种系列"。

    series 在库里就是逐款登记的产品名，那句话字面没错但等于没说，
    员工会读成"有 61 个系列"——生产上就是这么被指出来的。
    """
    items = [{"id": f"p{i}", "model": f"M{i}", "series": f"逐款系列{i}"} for i in range(61)]
    agent = BusinessAgent(_Catalog(items))

    reply = agent._answer_product_aggregate(_query("distinct_count", "series"), can_view_cost=False)

    assert reply is not None
    assert "逐款登记" in reply.content
    assert "61 种系列" not in reply.content
    # 「能统计什么」由本体生成（dimension_fields），不再是手写的"颜色、材质、档位"：
    # 以后新增维度字段会自动出现在提示里，不会和 schema 脱节。
    assert "想统计分类的话" in reply.content
    assert "颜色" in reply.content


def test_aggregate_without_a_field_returns_none_so_the_caller_clarifies() -> None:
    """聚合字段被校验丢掉时不猜，交给调用方去澄清。"""
    agent = BusinessAgent(_Catalog(_color_items()))

    reply = agent._answer_product_aggregate(
        ProductQuery(operation="aggregate"), can_view_cost=False
    )

    assert reply is None
