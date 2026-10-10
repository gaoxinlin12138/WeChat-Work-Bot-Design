"""列表渲染的篇幅：列表要能横向比较，不能每款吐一份完整资料。

生产现场：「镜面玻璃·镜面 3 款是哪三款」输出了 3 份完整资料、近 50 行
（每款都带"配件明细（共15项）"的 15 行）。款数一多就彻底没法看。

原因：没有可确认字段时，列表也退回了「完整资料」字段集。
完整资料用于单款 detail 是对的；列表的默认字段必须精简。

这里不逐个字段断言，而是直接盯住两件事：
  - 列表（list/search，多款）里绝不该出现"配件明细"
  - detail（单款完整资料）里应该有"配件明细"
"""

from app.business.product_query import ProductQuery
from app.business.product_store import ProductQueryResult
from app.business.service import BusinessAgent

COMPONENTS = [
    {"name": f"配件{index}", "supplier": f"供应商{index}"} for index in range(15)
]


class _Catalog:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items

    def search_products(self, *_args, **_kwargs):  # pragma: no cover
        return []

    def query_products_v2(self, query, *, include_sensitive):
        return ProductQueryResult(self.items, len(self.items), 1, len(self.items))


def _items(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"p{index}",
            "model": f"LY000{index}",
            "series": f"系列{index}",
            "color": "镜面玻璃·镜面",
            "material": "304钢壳铜水路",
            "set_price": 218.0,
            "body_price": 148.0,
            "supplier": "洁作",
            "notes": "10只装",
            "components": COMPONENTS,
        }
        for index in range(1, count + 1)
    ]


def _render(operation: str, count: int, *, allow_overview: bool = False) -> str:
    agent = BusinessAgent(_Catalog(_items(count)))
    result = ProductQueryResult(_items(count), count, 1, count)
    reply = agent._format_product_v2(
        result,
        ProductQuery(operation=operation, allow_overview=allow_overview),  # type: ignore[arg-type]
        can_view_cost=False,
    )
    return str(reply.content)


def test_a_multi_product_listing_does_not_dump_the_component_breakdown() -> None:
    """3 款一起列出时不该出现配件明细——那是完整资料的内容。"""
    content = _render("list", 3)

    assert "配件明细" not in content
    assert "配件1" not in content
    # 而且总行数要是"能一眼扫完"的量级，不是每款 20 行
    assert len(content.splitlines()) < 20


def test_a_listing_still_shows_the_fields_people_compare() -> None:
    """精简不等于没信息：型号、颜色、材质、套装价这些要留着。"""
    content = _render("list", 3)

    for expected in ("LY0001", "镜面玻璃·镜面", "304钢壳铜水路", "218.00"):
        assert expected in content


def test_a_single_product_detail_still_shows_the_full_record() -> None:
    """完整资料只对单款 detail 生效，配件明细在那里是应该出现的。

    （detail 且员工没说要看什么时会被守卫拦下要求澄清；"完整资料"走 allow_overview。）
    """
    content = _render("detail", 1, allow_overview=True)

    assert "配件明细" in content


def test_the_listing_length_does_not_grow_with_each_extra_field() -> None:
    """款数增加时，篇幅应按"每款固定几行"增长，不是按完整资料增长。"""
    three = _render("list", 3).splitlines()
    six = _render("list", 6).splitlines()

    per_item = (len(six) - len(three)) / 3
    assert per_item <= 6, f"每款占了 {per_item} 行，列表太啰嗦"
