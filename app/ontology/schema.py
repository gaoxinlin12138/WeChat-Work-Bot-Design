"""领域本体：把"系统知道的东西"集中写在一处，并由代码校验。

为什么需要（都是生产上真实踩过的）：

1. **员工问「有几个系列」，系统答「61 款产品一共有 61 种系列」** —— 因为 `series`
   在库里是逐款登记的产品名。数据库只知道"有一列叫 series"，
   不知道"它不是分类维度"。**数据库管"值"，本体管"意思"。**

2. **「镜面玻璃·镜面」是颜色取值，模型却把它猜成 `series` / `description`** ——
   模型知道字段"名"，没有字段"取值"的概念。本体声明每个属性的取值，
   值→字段才能确定性判定（见 `values.py`）。

务实本体的标准：**每一条声明都能指向一处代码或一条测试**，否则就是装饰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# 属性在领域里扮演的角色。这不是分类学，是**能不能做某件事**的依据：
#   identifier —— 每行一个值（型号、系列名）。对它统计"多少种"等于重复行数，无意义。
#   dimension  —— 低基数的分类维度（颜色 12 种、材质 10 种）。可以分类、筛选、统计。
#   measure    —— 数值度量（套装价、件数）。可以排序、比大小、求平均。
#   text       —— 自由文本（描述）。只能包含匹配。
#   json/media —— 结构化明细与媒体，不能筛选排序。
FieldRole = Literal["identifier", "dimension", "measure", "text", "json", "media"]

# 允许做去重计数的角色：只有分类维度与度量有意义。
AGGREGATABLE_ROLES: frozenset[str] = frozenset({"dimension", "measure"})


@dataclass(frozen=True)
class ProductFieldDefinition:
    label: str
    value_type: Literal["text", "number", "json", "media"]
    description: str
    aliases: tuple[str, ...]
    filterable: bool = True
    sensitive: bool = False
    patterns: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    # 领域角色：决定"能不能拿来分类/统计/排序"。默认 text 最保守。
    role: FieldRole = "text"
    # 派生属性：库里没有这一列，由其它字段算出来（见 DERIVED_DIMENSIONS）
    derived: bool = False
    # None means infer from the value type; explicit values document exceptions.
    sortable: bool | None = None
    permission_scope: str | None = None

    @property
    def aggregatable(self) -> bool:
        """能不能对它做"有多少种"这类统计。"""
        return self.role in AGGREGATABLE_ROLES

    @property
    def groupable(self) -> bool:
        """能不能作为分类维度分组（标识符不行，见 FieldRole 的注释）。"""
        return self.role == "dimension"

    @property
    def can_sort(self) -> bool:
        return self.sortable if self.sortable is not None else self.value_type in {"text", "number"}

    @property
    def required_permission(self) -> str | None:
        return self.permission_scope if self.sensitive else None


PRODUCT_FIELD_CATALOG: dict[str, ProductFieldDefinition] = {
    "model": ProductFieldDefinition(
        "产品型号",
        "text",
        "产品的正式完整型号",
        ("型号",),
        patterns=(r"(?:哪些|所有|全部|列出).{0,6}产品",),
        # 每款一个型号：数它等于数产品，不构成分类
        role="identifier",
    ),
    "series": ProductFieldDefinition(
        "系列",
        "text",
        "产品所属系列",
        ("系列",),
        # 实测 61 款产品有 61 个不同的 series（"621镀枪灰数显四档套装 B款"…），
        # 那是逐款登记的产品名，不是分类维度。所以它是标识符，不能聚合。
        # 业务上真正的"系列"见派生维度 family。
        role="identifier",
    ),
    "description": ProductFieldDefinition(
        "产品描述", "text", "产品公开描述", ("产品描述", "描述", "介绍"), role="text"
    ),
    "material": ProductFieldDefinition(
        "材质",
        "text",
        "产品主体本身使用的材质，不是套装配件",
        ("材质", "材料", "用料"),
        patterns=(
            r"(?:主体|本体|产品|它|这款).{0,6}(?:哪些|什么).{0,3}(?:材料|材质|用料).{0,4}(?:组成|构成|制成)",
            r"(?:主体|本体|产品|它|这款)?(?:是)?(?:由|用|用了|采用|使用).{0,6}(?:什么|哪些).{0,6}(?:做|制成|制作|构成|组成)",
        ),
        exclusions=(
            r"套装(?!.*(?:主体|本体|材质|材料|用料))",
            r"(?:由|用).*(?:配件|附件).*(?:组成|构成)",
        ),
        role="dimension",
    ),
    "spray_modes": ProductFieldDefinition(
        "档位", "text", "产品登记的档位", ("档位", "几档"), role="dimension"
    ),
    "color": ProductFieldDefinition(
        "颜色", "text", "产品颜色", ("颜色", "什么色", "配色"), role="dimension"
    ),
    "special_function": ProductFieldDefinition(
        "特殊功能",
        "text",
        "产品登记的特殊功能",
        ("特殊功能", "功能", "支持", "带有", "具有"),
        patterns=(r"(?:支持|带有|具有).{1,12}", r"(?:有|带).{1,10}(?:功能|的产品)"),
        role="dimension",
    ),
    "shipping_origin": ProductFieldDefinition(
        "出货地", "text", "产品出货地点", ("出货地", "发货地", "哪里发货"), role="dimension"
    ),
    "set_price": ProductFieldDefinition(
        "套装价",
        "number",
        "整套产品的公开销售价格",
        # 「贵」「便宜」是员工比较价格时最自然的说法。此前别名表里只有
        # 套装价/价格/多少钱/报价/售价，于是「A 和 B 哪个贵」里模型提出的
        # set_price（证据「哪个贵」）被判成没有依据、字段被移除，
        # V2 只能回「无法可靠确认你要查询的字段」——而两种模式下的正确答案都是价格。
        ("套装价", "价格", "多少钱", "报价", "售价", "贵", "便宜"),
        patterns=(
            r"(?:哪个|哪款|哪边).{0,4}(?:更)?(?:贵|便宜)",
            r"(?:价格|报价).{0,3}(?:高|低|贵|便宜)",
            # 员工说的是"300块以内""500元左右"这种**带单位的金额**，而不是"价格"二字。
            # 以前没有这条，于是模型给的 set_price 筛选（值 300、依据"300块以内"）
            # 被判成没有依据 → 回"这个查询条件无法安全执行"。
            # 要求必须带 元/块，避免和"3 件以内""2 档以内"这类数量词撞车。
            r"\d+(?:\.\d+)?\s*(?:元|块钱|块)(?:\s*(?:以内|以下|之内|以上|以外|左右|上下))?",
        ),
        exclusions=(r"主体|成本|采购|进价",),
        role="measure",
    ),
    "body_price": ProductFieldDefinition(
        "主体价",
        "number",
        "单独产品主体的公开价格",
        ("主体价", "主体多少钱"),
        patterns=(r"主体.{0,4}(?:价格|报价|售价|多少钱)",),
        role="measure",
    ),
    "cost_price": ProductFieldDefinition(
        "成本价",
        "number",
        "内部成本价格，只有员工明确询问且通过部门权限后才可返回",
        ("成本价", "成本", "进价", "采购价"),
        sensitive=True,
        role="measure",
        permission_scope="product.cost.read",
    ),
    "piece_count": ProductFieldDefinition(
        "件数", "number", "当前登记件数", ("件数", "库存", "多少件", "几件"), role="measure"
    ),
    "notes": ProductFieldDefinition(
        "备注",
        "text",
        "产品登记备注，实际内容是装箱规格（如 5只装 / 10只装 / 12只装）",
        # 「几只装」是客户和销售最自然的问法。登记在 piece_count（件数）之外的独立字段里，
        # 而 61 款产品的件数大多未登记、备注却全部有值，所以这个问题必须由备注来回答。
        # 不写「几件装」是为了避免和 piece_count 的别名「几件」撞车。
        ("备注", "几只装", "几只一装", "装箱", "包装规格"),
        # 只有 5 种取值（5只装/10只装/12只装…），能分类；
        # 但里面可能混着内部单价，所以同时是敏感字段（渲染时脱敏）。
        sensitive=True,
        role="dimension",
        permission_scope="product.internal.read",
    ),
    "supplier": ProductFieldDefinition(
        "供应商",
        "text",
        "套装供应商",
        ("供应商",),
        exclusions=(r"主体供应商",),
        role="dimension",
    ),
    "body_supplier": ProductFieldDefinition(
        "主体供应商", "text", "主体供应商", ("主体供应商",), role="dimension"
    ),
    "components": ProductFieldDefinition(
        "配件",
        "json",
        "整套产品包含的配件清单，不代表主体材质",
        ("配件", "标配", "附件", "清单"),
        patterns=(r"套装.{0,4}(?:包含|包括|有什么|有哪些|由什么组成|由哪些组成)",),
        exclusions=(r"套装.{0,6}(?:材料|材质|用料)",),
        role="json",
    ),
    "images": ProductFieldDefinition(
        "图片",
        "media",
        "已发布的产品图、主体图或配置图",
        ("图片", "产品图", "主体图", "配置图"),
        False,
        role="media",
    ),
    "videos": ProductFieldDefinition(
        "视频", "media", "已发布的产品视频", ("视频", "产品视频", "安装视频"), False, role="media"
    ),
}


# ---------------------------------------------------------------- 派生维度

@dataclass(frozen=True)
class DerivedDimension:
    """库里没有这一列，但业务上存在，由其它字段算出来。

    为什么需要：员工问"有几个系列"，而 `series` 是逐款产品名（61 款 61 个）。
    业务上他们说的"系列"其实是**型号族群**（LY0653GDT / LY0653NBT / LY0653GDT-T
    都属于 LY0653）。把这条业务规则写进本体，那个问题才有真答案。
    """

    key: str
    label: str
    source: str
    expr: str
    # 面向员工的说法（会进回复正文），与给开发看的 description 分开
    explain: str = ""
    description: str = ""

    def compute(self, item: dict[str, object]) -> str | None:
        raw = item.get(self.source)
        if raw is None:
            return None
        match = re.search(self.expr, str(raw))
        return match.group(1) if match else None


DERIVED_DIMENSIONS: dict[str, DerivedDimension] = {
    "family": DerivedDimension(
        key="family",
        label="系列",
        source="model",
        # LY0594GT1 → LY0594；LY0601GT-Cu → LY0601
        expr=r"^([A-Za-z]+\d{4})",
        explain="型号前 4 位相同即同一系列（如 LY0594GT1 与 LY0594NBT）",
        description="型号族群：型号前缀（字母 + 4 位数字）",
    ),
}


# ---------------------------------------------------------------- 索引与查询

PRODUCT_QUERY_FIELDS = frozenset(PRODUCT_FIELD_CATALOG)
PRODUCT_FILTER_FIELDS = frozenset(
    key for key, definition in PRODUCT_FIELD_CATALOG.items() if definition.filterable
)


def field_role(name: str) -> FieldRole:
    definition = PRODUCT_FIELD_CATALOG.get(name)
    return definition.role if definition is not None else "text"


def is_aggregatable(name: str) -> bool:
    """能不能对字段做"有多少种"这类统计。

    标识符（型号、系列名）不行 —— 数它等于数产品，"61 款产品有 61 种系列"是废答案。
    """
    definition = PRODUCT_FIELD_CATALOG.get(name)
    return bool(definition and definition.aggregatable)


def identifier_fields() -> tuple[str, ...]:
    return tuple(name for name, item in PRODUCT_FIELD_CATALOG.items() if item.role == "identifier")


def dimension_fields() -> tuple[str, ...]:
    return tuple(name for name, item in PRODUCT_FIELD_CATALOG.items() if item.groupable)
