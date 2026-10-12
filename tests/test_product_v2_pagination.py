"""V2 目录翻页的回归测试。

V2 是唯一产品查询链路，翻页上下文由结构化查询条件和页码维护。
"""

from app.business.product_query import ProductFieldRequest, ProductQuery, ProductReference
from app.business.product_store import ProductQueryResult
from app.business.service import BusinessAgent
from app.business.tools.product import ProductToolMixin


class _CatalogStore:
    """按页码切片的内存产品库，行为对齐 query_products_v2 的分页语义。"""

    def __init__(self, models: list[str]) -> None:
        self.models = models
        self.calls: list[int] = []

    def search_products(self, *_args, **_kwargs):
        return []

    def query_products_v2(self, query, *, include_sensitive):
        page = query.page or 1
        size = query.page_size or 5
        self.calls.append(page)
        start = (page - 1) * size
        items = [
            {"id": model, "model": model, "set_price": 100.0}
            for model in self.models[start : start + size]
        ]
        return ProductQueryResult(items, len(self.models), page, size)

    def get_active_product(self, _identifier):  # pragma: no cover - 本文件不触发
        return None


def _list_query() -> ProductQuery:
    """与真实计划同形：清单类提问带一个字段请求，证据取自原话。

    不能省掉字段请求：没有依据的字段会被 V2 校验全部移除，于是请求根本走不到分页那一步，
    测出来的就不是分页行为了。
    """
    return ProductQuery(
        operation="list",
        fields=("model",),
        requested_fields=(ProductFieldRequest("model", "哪些产品", source="fallback"),),
        page=1,
        page_size=5,
    )


def _reference(model: str) -> ProductReference:
    return ProductReference(model, "exact", model)


MODELS = [f"LY{i:04d}GT" for i in range(1, 13)]


def test_v2_list_records_catalog_context() -> None:
    """正式 V2 查询会写入可复用的目录上下文。"""
    agent = BusinessAgent(_CatalogStore(MODELS))

    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )
    context = agent._state_get(agent._product_catalog_context, "u1")
    assert isinstance(context, dict)
    assert context["mode"] == "v2"
    assert context["page"] == 1
    assert context["total"] == len(MODELS)


def test_v2_catalog_page_reply_pages_forward_and_back() -> None:
    store = _CatalogStore(MODELS)
    agent = BusinessAgent(store)
    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    forward = agent._v2_catalog_page_reply("u1", "下一页", can_view_cost=False)
    assert forward is not None
    assert "第 2/3 页" in forward.content
    assert "LY0006GT" in forward.content

    back = agent._v2_catalog_page_reply("u1", "上一页", can_view_cost=False)
    assert back is not None
    assert "第 1/3 页" in back.content
    assert "LY0001GT" in back.content


def test_v2_catalog_page_reply_stops_at_the_edges() -> None:
    agent = BusinessAgent(_CatalogStore(MODELS))
    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    first = agent._v2_catalog_page_reply("u1", "上一页", can_view_cost=False)
    assert first is not None
    assert "第一页" in first.content


def test_v2_catalog_page_reply_ignores_non_page_turns() -> None:
    agent = BusinessAgent(_CatalogStore(MODELS))
    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    assert agent._v2_catalog_page_reply("u1", "LY0653GDT 的材质", can_view_cost=False) is None


def test_v2_catalog_page_reply_needs_a_v2_context() -> None:
    """不符合 V2 结构的历史上下文不会被翻页逻辑误用。"""
    agent = BusinessAgent(_CatalogStore(MODELS))
    agent._product_catalog_context["u1"] = {"ids": ["a", "b"], "page": 1}

    assert agent._v2_catalog_page_reply("u1", "下一页", can_view_cost=False) is None


def test_absolute_page_number_jumps_to_that_page() -> None:
    """「第N页」是绝对页码，与「下一页」不同，两者都要支持。"""
    agent = BusinessAgent(_CatalogStore(MODELS))
    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    jumped = agent._v2_catalog_page_reply("u1", "第3页", can_view_cost=False)
    assert jumped is not None
    assert "第 3/3 页" in jumped.content


def test_absolute_page_number_out_of_range_reports_page_count() -> None:
    agent = BusinessAgent(_CatalogStore(MODELS))
    agent._answer_product_v2(
        "u1",
        "你们有哪些产品",
        _list_query(),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    beyond = agent._v2_catalog_page_reply("u1", "第99页", can_view_cost=False)

    assert beyond is not None
    assert "只有 3 页" in beyond.content


def test_page_number_without_a_catalog_is_not_a_product_page_turn() -> None:
    """没有产品清单时，「第2页」仍然是制度 PDF 的页码问题。

    绝对页码支持必须依附于「已存在产品目录上下文」，否则会把这条原有语义吃掉。
    """
    agent = BusinessAgent(_CatalogStore(MODELS))

    assert agent._catalog_page_requested("u1", "第2页") is False
    assert agent._v2_catalog_page_reply("u1", "第2页", can_view_cost=False) is None


def test_page_number_direction_and_target_parsing() -> None:
    assert ProductToolMixin._catalog_page_direction("下一页") == 1
    assert ProductToolMixin._catalog_page_direction("上一页") == -1
    assert ProductToolMixin._catalog_page_direction("第3页") is None
    assert ProductToolMixin._catalog_page_target("第3页") == 3
    assert ProductToolMixin._catalog_page_target("第 12 页") == 12
    assert ProductToolMixin._catalog_page_target("下一页") is None
