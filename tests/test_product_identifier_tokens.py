"""型号提取与「价格/数量不是型号」的回归测试。

生产现场（审计记录 14:00）：
  灰玻璃·喷灰颜色，价格在150左右的有哪些产品
    → 引用=['150']（价格被当成型号）
    → 过滤器是对的：[color contains 灰玻璃·喷灰, set_price between 135~165]
    → 拿「型号含 150」去查库 → 0 款 → 答「产品库中没有找到对应产品」
  而实际有 3 款（140 / 144 / 155）。

原逻辑只排除「紧跟数字后面的单位」（元/个/件…），于是「150左右」这种不带单位的写法
漏了进来。`_ground_product_query` 本来就会丢掉「原话里没有的引用」，
只是它把价格数字误判成了原话里的标识符。
"""

import pytest

from app.business.product_selection import (
    has_product_identifier,
    product_identifier_tokens,
)


@pytest.mark.parametrize(
    "query",
    [
        "灰玻璃·枪灰颜色，价格在150左右的有哪些产品",
        "灰玻璃·喷灰颜色，价格在150左右的有哪些产品",
        "150元左右的产品",
        "预算500以内的产品",
        "价格 150",
        "价格在150",
        "售价大概是200",
        "200以下的产品",
    ],
)
def test_price_expressions_are_not_model_identifiers(query: str) -> None:
    assert product_identifier_tokens(query) == []
    assert has_product_identifier(query) is False


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("LY0653GDT 的价格", ["ly0653gdt"]),
        ("0653 的价格", ["0653"]),
        ("0653价格多少", ["0653"]),
        ("LTA-822 现在卖多少钱", ["lta-822"]),
        ("LY150 的材质", ["ly150"]),
    ],
)
def test_model_tokens_still_survive(query: str, expected: list[str]) -> None:
    """含字母的写法（LY150）和真正的型号片段（0653）不受影响。

    排除规则只作用于**纯数字**，否则带数字的型号会被误伤。
    """
    assert product_identifier_tokens(query) == expected


def test_unit_suffixes_were_already_excluded() -> None:
    """原有能力不能退化：紧跟单位的数字仍被排除。"""
    assert product_identifier_tokens("50元的产品") == []
    assert product_identifier_tokens("3个装") == []
