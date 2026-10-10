"""筛选值解析：值→字段 与 写法归一。

为什么需要：员工给的是"值"，不是"字段"。模型没有取值概念，只能猜它属于哪一列。
实测同一个颜色「镜面玻璃·镜面」被猜成 description、series（都返回 0 款），
而「灰玻璃·枪灰」猜成 color 猜对了——同一个问题时对时错，全靠运气。

这个模块用**库里的取值目录**把值钉到正确的字段上。规则刻意保守：
只在能唯一确定时才动手，其余原样保留（不替员工做决定）。
"""

import pytest

from app.business.planner import ProductFilter, ProductQuery
from app.business.service import BusinessAgent

CATALOG = {
    "color": ("灰玻璃·枪灰", "奶白玻璃·奶白", "镜面玻璃·镜面", "枪灰", "镀枪灰"),
    "material": ("全铜", "ABS外壳铜水路", "304钢壳铜水路"),
    "series": ("电镀0685三档镜面淋浴套装", "枪灰0653四档套装"),
    "description": ("手柄手轮三档无数显氛围灯大置物花洒",),
    "notes": ("10只装", "5只装"),
}


class _Catalog:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def search_products(self, *_args, **_kwargs):  # pragma: no cover
        return []

    def product_text_field_values(self):
        if self.fail:
            raise RuntimeError("目录查不动")
        return CATALOG


def _resolve(filters: tuple[ProductFilter, ...], *, fail: bool = False):
    agent = BusinessAgent(_Catalog(fail=fail))
    return agent._resolve_product_filter_values(ProductQuery(operation="list", filters=filters))


def _only(query: ProductQuery) -> ProductFilter:
    assert len(query.filters) == 1
    return query.filters[0]


def test_a_value_on_the_wrong_field_is_moved_to_the_field_that_owns_it() -> None:
    """模型把颜色值猜成了 series / description —— 实测就是这么错的。"""
    for wrong_field in ("series", "description"):
        item = ProductFilter(wrong_field, "contains", "镜面玻璃·镜面", "镜面玻璃·镜面")

        resolved = _only(_resolve((item,)))

        assert resolved.field == "color"
        assert resolved.value == "镜面玻璃·镜面"


def test_a_value_written_without_separators_is_normalized_to_the_stored_form() -> None:
    """员工漏打间隔号（"灰玻璃枪灰"）要归一到库里的写法，否则静默返回 0 款。"""
    item = ProductFilter("color", "contains", "灰玻璃枪灰", "灰玻璃枪灰")

    resolved = _only(_resolve((item,)))

    assert resolved.field == "color"
    assert resolved.value == "灰玻璃·枪灰"


def test_an_ambiguous_value_is_left_alone() -> None:
    """"枪灰"同时命中枪灰/镀枪灰/灰玻璃·枪灰——不该由代码替员工挑一个。

    （contains 本来就能命中这三者，保持原样反而是对的。）
    """
    item = ProductFilter("color", "contains", "枪灰", "枪灰")

    resolved = _only(_resolve((item,)))

    assert resolved == item


def test_a_value_that_matches_nothing_is_left_alone() -> None:
    item = ProductFilter("color", "contains", "不存在颜色", "不存在颜色")

    resolved = _only(_resolve((item,)))

    assert resolved == item


def test_numeric_and_range_filters_are_never_rewritten() -> None:
    """数值区间是模型算出来的，改写会破坏语义。"""
    item = ProductFilter("set_price", "between", [135, 165], "价格在150左右")

    resolved = _only(_resolve((item,)))

    assert resolved == item


def test_a_filter_already_on_the_right_field_is_kept_when_it_already_matches() -> None:
    item = ProductFilter("material", "contains", "全铜", "全铜")

    resolved = _only(_resolve((item,)))

    assert resolved == item


def test_a_catalog_lookup_failure_does_not_break_the_query() -> None:
    """目录查不动时按"没有目录"处理，不能让回答失败。"""
    item = ProductFilter("series", "contains", "镜面玻璃·镜面", "镜面玻璃·镜面")

    resolved = _only(_resolve((item,), fail=True))

    assert resolved == item


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_values_are_ignored(value: str) -> None:
    item = ProductFilter("color", "equals", value, value)

    resolved = _only(_resolve((item,)))

    assert resolved == item


def test_several_filters_are_resolved_independently() -> None:
    """一句里既有写错字段的颜色、也有写错写法的颜色，两个都要修好。"""
    query = _resolve(
        (
            ProductFilter("series", "contains", "镜面玻璃·镜面", "镜面玻璃·镜面"),
            ProductFilter("color", "contains", "灰玻璃枪灰", "灰玻璃枪灰"),
        )
    )

    assert [(item.field, item.value) for item in query.filters] == [
        ("color", "镜面玻璃·镜面"),
        ("color", "灰玻璃·枪灰"),
    ]
