"""只从已登记字段识别产品对象和变体，不把检索排序当作员工的选择。"""

import re
from collections.abc import Sequence
from typing import Protocol

from app.retrieval import normalize_text


class ProductLike(Protocol):
    source_id: str
    content: str


def product_fields(content: str) -> dict[str, str]:
    return {
        key.strip(): value.strip().removesuffix("。")
        for field in content.split("；")
        if "：" in field
        for key, value in [field.split("：", 1)]
    }


def mentions_identifier(query: str, identifier: str) -> bool:
    value = normalize_text(identifier).replace(" ", "")
    if not value:
        return False
    query = normalize_text(query)
    # 数字/英文编号必须完整匹配，0689 不能命中 10689 或 0689-A。
    return bool(re.search(r"(?<![a-z0-9-])" + re.escape(value) + r"(?![a-z0-9-])", query))


def has_product_identifier(query: str) -> bool:
    normalized = normalize_text(query)
    for match in re.finditer(r"(?<![a-z0-9])[a-z0-9]+(?:[.-][a-z0-9]+)*(?![a-z0-9])", normalized):
        token = match.group()
        if len(token) < 2 or not any(char.isdigit() for char in token):
            continue
        # 数量、金额和尺寸不是型号，不能据此拒绝正常产品筛选。
        if re.match(
            r"\s*(?:元|块|套|个|件|台|箱|条|厘米|毫米|cm|mm|米|%)",
            normalized[match.end() :],
        ):
            continue
        return True
    return False


def narrow_variants[T: ProductLike](query: str, products: Sequence[T]) -> list[T]:
    """对员工明确提到的差异取交集；冲突条件不能靠最高分选中一个产品。"""
    if len(products) < 2:
        return list(products)
    normalized = normalize_text(query).replace(" ", "")
    values_by_id: dict[str, list[str]] = {}
    for product in products:
        fields = product_fields(product.content)
        values_by_id[product.source_id] = [
            normalize_text(fields[key]).replace(" ", "")
            for key in (
                "产品型号",
                "系列",
                "产品描述",
                "款式",
                "材质",
                "档位",
                "颜色",
                "特殊功能",
                "出货地",
            )
            if fields.get(key) and fields[key] not in {"无", "未登记"}
        ]
    # 取 query 与已登记字段共有的连续片段，广东可匹配广东佛山；仅使用能区分候选的片段。
    matched: dict[str, set[str]] = {}
    for token in re.findall(r"[\u3400-\u9fff]+|[a-z0-9-]+", normalized):
        for size in range(min(len(token), 24), 1, -1):
            for index in range(len(token) - size + 1):
                term = token[index : index + size]
                ids = {
                    key
                    for key, values in values_by_id.items()
                    if any(term in value for value in values)
                }
                if ids:
                    matched[term] = ids
    # 只保留最长的已登记片段，避免短词覆盖更具体的规格描述。
    maximal = [
        term for term in matched if not any(term != other and term in other for other in matched)
    ]
    selected = set(values_by_id)
    for term in maximal:
        selected.intersection_update(matched[term])
    return [product for product in products if product.source_id in selected]


def choice_index(query: str) -> int | None:
    normalized = normalize_text(query).strip(" 。！？!?")
    match = re.fullmatch(r"(?:选|选择|要)?\s*(\d{1,3})", normalized)
    if match:
        return int(match.group(1)) - 1
    match = re.search(r"第([一二两三四五六七八九十百\d]+)(?:个|款|项|种)", normalized)
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        return int(value) - 1
    numbers = dict(zip("一二两三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
    if value == "十":
        return 9
    if "十" in value:
        tens, units = value.split("十", 1)
        if (not tens or tens in numbers) and (not units or units in numbers):
            return numbers.get(tens, 1) * 10 + numbers.get(units, 0) - 1
    return numbers[value] - 1 if value in numbers else None


def is_product_follow_up(query: str) -> bool:
    """仅省略对象的短问沿用已选 ID；显式新型号/名称始终重新查询。"""
    normalized = normalize_text(query)
    if has_product_identifier(normalized) or len(normalized) > 40:
        return False
    remainder = re.sub(
        r"产品视频|安装视频|安装教程|产品图片|白底主图|配置图|产品价格|主体价|"
        r"套装价|出货地|销售价格|实物照片|"
        r"白底图|实物图|配置图|产品图|价格|报价|售价|多少钱|参数|规格|配件|"
        r"图片|照片|主图|视频|颜色|材质|款式|档位|税费|运费|发给我|给我看|发我|发送|"
        r"看一下|看下|看看|播放|打开|这个产品|那个产品|这款产品|这款|那款|"
        r"这个|那个|它|上面|刚才|当前|产品|有没有|有多少|有哪些|多少|几张|几条|"
        r"哪里|什么|啥|怎么样|详细介绍|介绍一下|查一下|查询|那就|还有|一共|总共|"
        r"的|也|呢|吗|啊|呀|吧|把|请|想|要|和|那|都|带|有|、|，|。|？|！|\s|[?!,.]",
        "",
        normalized,
    )
    return not remainder and bool(normalized)
