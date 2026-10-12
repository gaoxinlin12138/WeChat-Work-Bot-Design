"""V2 字段渲染与概览判定的回归测试。

两个都是「不会让其它测试变红」的静默缺陷：
- 概览判定词表漏了「参数」「规格」，于是「LY0653GDT 的全部参数」被判成没说字段；
- 字段→数据库列的映射写在字典展开之前，被同名映射覆盖，产品描述永远取不到值。
"""

from app.business.intent import _PRODUCT_OVERVIEW_WORDS
from app.business.product_query import ProductQuery, ProductReference, is_overview_request
from app.business.product_store import ProductQueryResult
from app.business.service import BusinessAgent


class _Store:
    def search_products(self, *_args, **_kwargs):  # pragma: no cover - 本文件不触发检索
        return []


def _reference(model: str) -> ProductReference:
    return ProductReference(model, "exact", model)


def test_overview_vocabulary_covers_every_declared_overview_phrase() -> None:
    """意图层认定的概览说法，概览判定必须同样认识。

    「参数」「规格」曾只在意图路由词表里，于是这类提问在 V2 下被判成「没说要哪些字段」
    并直接拒答。
    """
    unrecognized = [
        phrase
        for phrase in _PRODUCT_OVERVIEW_WORDS
        if not is_overview_request(f"LY0653GDT {phrase}", (_reference("LY0653GDT"),))
    ]

    assert unrecognized == [], f"概览判定不认识这些说法：{unrecognized}"


def test_all_parameters_phrasing_counts_as_overview() -> None:
    for phrase in ("全部参数", "所有参数", "的参数", "完整资料", "详细规格", "产品配置"):
        assert is_overview_request(f"LY0653GDT {phrase}", (_reference("LY0653GDT"),)), phrase


def test_an_unrelated_tail_is_not_an_overview_request() -> None:
    """概览判定不能放得太宽，否则「没说字段」会被当成「要全部」。"""
    assert not is_overview_request(
        "LY0653GDT 和 LY0689GT 哪个贵", (_reference("LY0653GDT"),)
    )
    assert not is_overview_request("LY0653GDT 材质呢", (_reference("LY0653GDT"),))


def test_v2_overview_renders_the_product_description() -> None:
    """字段名与数据库列名不一致时，映射不能被同名映射覆盖掉。"""
    agent = BusinessAgent(_Store())
    description = "钢琴四档数显氛围灯大置物套装花洒"
    result = ProductQueryResult(
        items=[{"id": "p1", "model": "LY0653GDT", "product_description": description}],
        total=1,
        page=1,
        page_size=20,
    )
    query = ProductQuery(
        operation="detail",
        references=(_reference("LY0653GDT"),),
        allow_overview=True,
    )

    reply = agent._format_product_v2(result, query, can_view_cost=False)

    # 未登记的字段本来就会显示「资料未登记」，所以只断言描述这一行取到了真实值。
    assert f"产品描述：{description}" in reply.content


def test_v2_renders_notes_so_the_packing_spec_is_answerable() -> None:
    """「几只装」要靠备注回答：61 款产品的件数大多未登记，备注却全部有值。"""
    agent = BusinessAgent(_Store())
    result = ProductQueryResult(
        items=[{"id": "p1", "model": "LY0653GDT", "notes": "5只装"}],
        total=1,
        page=1,
        page_size=20,
    )
    query = ProductQuery(
        operation="detail",
        references=(_reference("LY0653GDT"),),
        allow_overview=True,
    )

    reply = agent._format_product_v2(result, query, can_view_cost=False)

    assert "备注：5只装" in reply.content


def test_v2_note_hides_internal_pricing_without_cost_permission() -> None:
    """备注里混着供应商和单价，没有成本权限时不能跟着显示出来。

    5 款产品的备注形如「10只装；止水阀*2：供应商圣冠，单价0.27元」。
    V2 直接读取整列，因此必须在输出前过滤内部供应商和单价信息。
    """
    agent = BusinessAgent(_Store())
    private_note = "10只装；止水阀*2：供应商圣冠，单价0.27元"
    result = ProductQueryResult(
        items=[{"id": "p1", "model": "LY0610GT", "notes": private_note}],
        total=1,
        page=1,
        page_size=20,
    )
    query = ProductQuery(
        operation="detail",
        references=(_reference("LY0610GT"),),
        allow_overview=True,
    )

    public = agent._format_product_v2(result, query, can_view_cost=False)
    assert "备注：10只装" in public.content
    assert "单价" not in public.content
    assert "圣冠" not in public.content

    # 有成本权限时给出完整备注，信息不丢
    with_cost = agent._format_product_v2(result, query, can_view_cost=True)
    assert "单价0.27元" in with_cost.content
