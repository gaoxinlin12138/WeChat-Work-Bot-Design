"""取值层（本体的个体）：字段 → 有哪些取值。

三件事都靠它：

1. **值 → 字段**：员工说"镜面玻璃·镜面"，系统要能确定它是颜色，
   而不是让模型在 color / series / description 里猜（实测猜错就是 0 款）。
2. **写法归一**："灰玻璃枪灰"（漏打间隔号）要能对上库里的"灰玻璃·枪灰"。
3. **喂给模型**：低基数字段的取值放进提示词，从源头减少乱猜。

**值一定从库里生成，绝不手写。** 手写的取值清单会随上架新产品而腐烂 ——
这是这一层的设计底线（模式手写、个体生成）。
"""

from __future__ import annotations

import re

from app.ontology.schema import PRODUCT_FIELD_CATALOG

# 需要纳入取值目录的字段：所有文本字段。
# 由模式推导而不是手写清单 —— 以后加字段自动纳入。
VALUE_CATALOG_FIELDS: tuple[str, ...] = tuple(
    name
    for name, definition in PRODUCT_FIELD_CATALOG.items()
    if definition.value_type == "text"
)

# 比较取值时忽略的字符：分隔符、标点、空白、括号。
# "灰玻璃枪灰" 与 "灰玻璃·枪灰" 必须视为同一个值，否则按值筛选会静默返回 0 款。
_SEPARATOR_PATTERN = re.compile(r"[\s·。.,，、/\\|+＋_\-—－()（）\[\]【】]")


def value_key(value: str) -> str:
    """取值的比较键：去掉分隔符与标点、统一大小写。"""
    return _SEPARATOR_PATTERN.sub("", value.casefold())


def matching_values(values: tuple[str, ...], text: str) -> list[str]:
    """在给定取值里找出与 text 匹配的写法（忽略分隔符与标点）。

    方向是"库里的取值包含员工写的值"：员工常写简称（"枪灰"），
    库里是完整写法（"灰玻璃·枪灰"），反向匹配就找不到了。
    """
    key = value_key(text)
    if not key:
        return []
    return [value for value in values if key in value_key(value)]


def fields_sharing_value(
    catalog: dict[str, tuple[str, ...]], text: str, *, exclude: str | None = None
) -> dict[str, list[str]]:
    """找出哪些字段的取值与 text 匹配（可排除某个字段）。

    用于"模型把值放在错误字段上"的纠正：只在**恰好一个**其它字段匹配时才动手，
    多个字段都匹配（"枪灰"命中枪灰/镀枪灰/灰玻璃·枪灰）就保持原样，不替员工做决定。
    """
    matched: dict[str, list[str]] = {}
    for field, values in catalog.items():
        if field == exclude:
            continue
        found = matching_values(values, text)
        if found:
            matched[field] = found
    return matched
