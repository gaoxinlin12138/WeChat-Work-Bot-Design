"""对比操作（compare）的回归测试。

compare 此前被声明、被识别、被校验通过，但执行时与 list 走同一条路径：
不做任何对比，还把两个不同型号冠以「XX系列目前有 N 款」的标题。
"""

from app.business.service import BusinessAgent
from app.business.store import BusinessResult
from app.business.tools.product import ProductToolMixin


class _Store:
    """只满足 BusinessAgent 构造的最小 store。"""

    def search_products(self, *_args, **_kwargs):  # pragma: no cover - 本文件不触发检索
        return []


def _product(model: str, set_price: str) -> BusinessResult:
    return BusinessResult(
        source_type="product",
        source_id=model,
        title=model,
        # product_fields 以「；」切分字段，这里必须用真实格式，否则字段解析不出来。
        content=f"产品型号：{model}；套装价：{set_price}",
        score=1.0,
    )


def _v2_product(model: str, set_price: str) -> dict[str, object]:
    return {"model": model, "set_price": set_price.removesuffix("元")}


def test_comparison_verdict_names_the_more_expensive_model() -> None:
    agent = BusinessAgent(_Store())
    products = [_v2_product("LY0653GDT", "234.00元"), _v2_product("LY0689GT", "258.00元")]

    verdict = agent._v2_comparison_verdict(products)

    assert verdict is not None
    assert "LY0689GT" in verdict
    assert "258.00" in verdict
    assert "LY0653GDT" in verdict


def test_comparison_verdict_reports_equal_prices() -> None:
    agent = BusinessAgent(_Store())
    products = [_v2_product("A-1", "100.00元"), _v2_product("B-1", "100.00元")]

    verdict = agent._v2_comparison_verdict(products)

    assert verdict is not None
    assert "相同" in verdict


def test_comparison_verdict_refuses_when_a_price_is_missing() -> None:
    """缺一个价格就不下结论——猜出来的对比比没有对比更危险。"""
    agent = BusinessAgent(_Store())
    products = [_v2_product("A-1", "100.00元"), _v2_product("B-1", "")]

    assert agent._v2_comparison_verdict(products) is None


def test_comparison_verdict_needs_at_least_two_priced_models() -> None:
    agent = BusinessAgent(_Store())

    assert agent._v2_comparison_verdict([]) is None
    assert agent._v2_comparison_verdict([_v2_product("A-1", "100.00元")]) is None


def test_full_model_number_wins_over_containing_variants() -> None:
    """员工写全型号时，精确命中必须优先于「只是包含它」的变体。

    否则「LY0653GDT」会同时命中 LY0653GDT-T / LY0653GDT-Y，
    一个两点对比被展开成一串同系列产品。
    """
    agent = BusinessAgent(_Store())
    products = [
        _product("LY0653GDT", "234.00元"),
        _product("LY0653GDT-T", "211.00元"),
        _product("LY0653GDT-Y", "234.00元"),
        _product("LY0689GT", "258.00元"),
    ]

    resolved, error = ProductToolMixin._resolve_product_references(
        agent, ("LY0653GDT", "LY0689GT"), products, purpose="对比"
    )

    assert error is None
    assert [item.title for item in resolved] == ["LY0653GDT", "LY0689GT"]
