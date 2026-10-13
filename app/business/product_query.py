"""Validated, model-facing product query contract.

字段目录的**唯一权威**已经搬到 `app.ontology.schema`（那里多了"领域角色"：
标识符 / 维度 / 度量 / 文本 / 媒体）。这里重新导出，供既有导入点继续使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from app.business.product_focus import ProductFocus
from app.business.reference_terms import REFERENCE_PRONOUN_SET
from app.ontology.schema import (
    PRODUCT_FIELD_CATALOG as _ONTOLOGY_FIELD_CATALOG,
)
from app.ontology.schema import (
    PRODUCT_FILTER_FIELDS as _ONTOLOGY_FILTER_FIELDS,
)
from app.ontology.schema import (
    PRODUCT_QUERY_FIELDS as _ONTOLOGY_QUERY_FIELDS,
)
from app.ontology.schema import (
    ProductFieldDefinition,  # noqa: F401  (re-export)
)

ProductOperation = Literal[
    "detail", "search", "list", "compare", "recommend", "media", "aggregate", "count"
]
ProductCountTarget = Literal["product"]
ProductFilterOperator = Literal[
    "equals",
    "contains",
    "is_present",
    "is_empty",
    "less_than",
    "less_than_or_equal",
    "greater_than",
    "greater_than_or_equal",
    "between",
    "in",
]
ProductReferenceKind = Literal["exact", "fragment", "context"]
ProductEvidenceSource = Literal["current", "context", "fallback"]
ProductResultScope = Literal["first", "all_matches"]
ProductContextMode = Literal["independent", "continuation"]
ProductSortDirection = Literal["asc", "desc"]


PRODUCT_FIELD_CATALOG = _ONTOLOGY_FIELD_CATALOG

PRODUCT_QUERY_FIELDS = _ONTOLOGY_QUERY_FIELDS
PRODUCT_FILTER_FIELDS = _ONTOLOGY_FILTER_FIELDS
_TEXT_OPERATORS = frozenset({"equals", "contains", "is_present", "is_empty", "in"})
_NUMBER_OPERATORS = frozenset(
    {
        "equals",
        "is_present",
        "is_empty",
        "less_than",
        "less_than_or_equal",
        "greater_than",
        "greater_than_or_equal",
        "between",
        "in",
    }
)


@dataclass(frozen=True)
class ProductReference:
    value: str
    kind: ProductReferenceKind = "fragment"
    evidence: str = ""
    source: ProductEvidenceSource = "current"


@dataclass(frozen=True)
class ProductFieldRequest:
    field: str
    evidence: str = ""
    source: ProductEvidenceSource = "current"


@dataclass(frozen=True)
class ProductFilter:
    field: str
    operator: ProductFilterOperator
    value: object = ""
    evidence: str = ""
    source: ProductEvidenceSource = "current"


@dataclass(frozen=True)
class ProductSort:
    field: str
    direction: ProductSortDirection = "asc"


# 聚合：员工问的不是"列出哪些"，而是"有多少种/最贵的是哪个/平均多少"。
# 这类问题以前只能退化成产品清单——模型理解对了也答不出来，因为执行层没有这个能力。
# 形状是可枚举的（不像员工的说法那样无穷），所以补一次就够了。
ProductAggregateKind = Literal["distinct_count", "max", "min", "average"]


@dataclass(frozen=True)
class ProductAggregate:
    kind: ProductAggregateKind
    field: str
    # distinct_count 时最多列出几种取值。
    # 默认取 30 是为了"能列完就列完"：种类只有十几种却截断到 10、再补一句"另有 2 种未列出"，
    # 等于既给了总数又藏了内容，员工只能再问一遍。
    limit: int = 30
    # 与字段/筛选同样要求"原话里有依据"，否则模型可以凭空聚合一个员工没问的字段
    evidence: str = ""
    source: ProductEvidenceSource = "current"


@dataclass(frozen=True)
class ProductAmbiguity:
    code: str
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class ProductQuery:
    """Versioned product semantics proposed by the model and enforced by Python."""

    operation: ProductOperation = "detail"
    include_models: tuple[str, ...] = ()
    exclude_models: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    filters: tuple[ProductFilter, ...] = ()
    version: int = 2
    references: tuple[ProductReference, ...] = ()
    requested_fields: tuple[ProductFieldRequest, ...] = ()
    result_scope: ProductResultScope = "all_matches"
    sorts: tuple[ProductSort, ...] = ()
    # 聚合请求（多少种/最贵/平均）；仅 operation == "aggregate" 时有意义
    aggregate: ProductAggregate | None = None
    page: int = 1
    page_size: int = 5
    context_mode: ProductContextMode = "independent"
    ambiguities: tuple[ProductAmbiguity, ...] = ()
    # Internal provenance, never accepted from model JSON. Survives plan transforms.
    original_fields: tuple[str, ...] = ()
    allow_overview: bool = False
    # Count has an explicit entity target so "多少种产品" cannot be confused
    # with distinct aggregation over a field.
    target: ProductCountTarget = "product"
    # Deterministic pre-parser provenance.  These fields are never accepted
    # from model JSON; they record conditions that the employee explicitly
    # supplied so the server can reject a model plan that silently widens the
    # query.  Rule-based plans may also copy them into the executable shape.
    required_references: tuple[ProductReference, ...] = ()
    required_filters: tuple[ProductFilter, ...] = ()
    required_operation: ProductOperation | None = None
    # Server-resolved result IDs.  This is never accepted from model JSON;
    # it is populated only after a trusted conversation focus resolves an
    # ordinal reference and survives graph/context serialization.
    resolved_product_ids: tuple[str, ...] = ()

    def effective_references(self) -> tuple[ProductReference, ...]:
        if self.references:
            return self.references
        return tuple(
            ProductReference(value=value, kind="fragment", evidence=value, source="fallback")
            for value in self.include_models
        )

    def effective_field_requests(self) -> tuple[ProductFieldRequest, ...]:
        if self.requested_fields:
            return self.requested_fields
        return tuple(ProductFieldRequest(field=value, source="fallback") for value in self.fields)


@dataclass(frozen=True)
class ProductValidationResult:
    query: ProductQuery
    errors: tuple[str, ...] = ()
    removed_fields: tuple[str, ...] = ()
    permission_denied_fields: tuple[str, ...] = ()
    ambiguities: tuple[ProductAmbiguity, ...] = ()


def _normalized(value: str) -> str:
    # Evidence is still required to be a contiguous quote; equivalent Chinese/
    # ASCII punctuation should not make the quote fail grounding.
    normalized = value.casefold().translate(str.maketrans({
        "？": "?", "！": "!", "，": ",", "。": ".", "：": ":", "；": ";",
        "（": "(", "）": ")", "【": "[", "】": "]", "“": '"', "”": '"',
    }))
    return re.sub(r"\s+", "", normalized)


def _evidence_is_grounded(evidence: str, message: str) -> bool:
    return bool(evidence.strip()) and _normalized(evidence) in _normalized(message)


def _field_supported_by_evidence(field_name: str, evidence: str) -> bool:
    return bool(field_semantic_spans(field_name, evidence))


def _positive_clauses(message: str) -> tuple[str, ...]:
    clauses = re.split(
        r"[，,。；;！？!?]|而是|(?<!不)是问|以及|并且|和|与|及", _normalized(message)
    )
    return tuple(
        clause for clause in clauses if not re.search(r"不是|不要|不用查|无需|别查|不问", clause)
    )


def field_semantic_spans(field_name: str, message: str) -> tuple[str, ...]:
    """Shared, declarative field semantics; retain positive clauses, not negated mentions."""
    definition = PRODUCT_FIELD_CATALOG[field_name]
    spans: list[str] = []
    for clause in _positive_clauses(message):
        if any(re.search(pattern, clause) for pattern in definition.exclusions):
            continue
        patterns = (*map(re.escape, definition.aliases), *definition.patterns)
        spans.extend(
            match.group() for pattern in patterns for match in re.finditer(pattern, clause)
        )
    return tuple(dict.fromkeys(spans))


def field_evidence_is_valid(field_name: str, evidence: str, message: str) -> bool:
    """Allow a literal semantic span or an anchored part of a shared semantic pattern."""
    if not _evidence_is_grounded(evidence, message):
        return False
    compact = _normalized(evidence)
    evidence_spans = field_semantic_spans(field_name, evidence)
    message_spans = field_semantic_spans(field_name, message)
    return any(
        span in compact or (len(compact) >= 2 and compact in span)
        for span in (*evidence_spans, *message_spans)
    )


def product_field_semantics_prompt() -> str:
    return "\n".join(
        f"{name}：{item.description}；表达：{'、'.join(item.aliases)}"
        + (f"；语义模式：{'；'.join(item.patterns)}" if item.patterns else "")
        for name, item in PRODUCT_FIELD_CATALOG.items()
    )


def ambiguity_choice(message: str) -> str | None:
    compact = _normalized(message).strip("。！？!?，,")
    positional = {
        "前者": "material",
        "第一个": "material",
        "后者": "components",
        "第二个": "components",
    }
    if compact in positional:
        return positional[compact]
    fields = {item.field for item in fallback_field_requests(message)}
    return next(iter(fields)) if len(fields) == 1 and fields <= {"material", "components"} else None


# 「把这款产品的资料都给我」这类请求的填充词。去掉型号之后，整句只由这些词组成，
# 就说明员工没有点名具体字段，而是要整体概览。
#
# 这里必须覆盖 intent 侧 _PRODUCT_OVERVIEW_WORDS 的每一个短语。此前「参数」「规格」
# 只出现在意图路由词表里、不在这里，于是「LY0653GDT 的全部参数」被判成「没说要哪些
# 字段」：结构化查询会拒绝无法从原话确认的字段，避免把模糊请求扩展成完整资料。
PRODUCT_OVERVIEW_FILLER_WORDS = (
    # 礼貌语与动作
    "查一下",
    "查询",
    "查看",
    "看看",
    "看下",
    "帮我",
    "给我",
    "提供",
    # 产品资料助手常见的交付动词；它们不指定字段，但明确要求把资料发来。
    # 例如「发我 LTA-822 的详细信息」不能因为没有显式字段而被误判为澄清。
    "发我",
    "发给我",
    "发送",
    "给我发",
    "一下",
    "请",
    "把",
    "将",
    "我想",
    "想",
    "要",
    # 指代与主体
    "这款",
    "那个",
    "这个",
    "那",
    "产品",
    "的",
    "呢",
    # 换主体/继续看。这些词本身不指定任何字段，去掉型号后整句只剩它们，
    # 说明员工是在换一个产品来看，而不是在问某个具体字段。
    "换成",
    "换个",
    "换",
    "再来",
    # 「要全部资料」的各种说法
    "完整",
    "全部",
    "所有",
    "详细",
    "基本",
    "介绍",
    "资料",
    "信息",
    "详情",
    "概览",
    "参数",
    "规格",
    "配置",
    "有什么",
    "有哪些",
)

_OVERVIEW_FILLER_PATTERN = re.compile(
    "(?:"
    + "|".join(
        re.escape(word)
        for word in sorted(PRODUCT_OVERVIEW_FILLER_WORDS, key=len, reverse=True)
    )
    + ")*"
)


def is_overview_request(message: str, references: tuple[ProductReference, ...]) -> bool:
    """Empty fields are not an overview request; require explicit overview words or a bare model."""
    if not references:
        return False
    remainder = _normalized(message)
    for reference in references:
        remainder = remainder.replace(_normalized(reference.value), "")
    remainder = remainder.strip("。！？!?，,、：:")
    return _OVERVIEW_FILLER_PATTERN.fullmatch(remainder) is not None


def is_overview_phrase(message: str) -> bool:
    """Check an overview-only phrase after its product reference was removed.

    Ordinal follow-ups use a context reference (for example, ``第五个``), which
    is not literally present in the current message as a product model.  Keep
    the vocabulary shared with explicit-model overview detection while requiring
    a non-empty tail so a bare ordinal does not silently become full detail.
    """
    remainder = _normalized(message).strip("。！？!?，,、：:")
    return bool(remainder) and _OVERVIEW_FILLER_PATTERN.fullmatch(remainder) is not None


def _filter_value_is_quoted(item: ProductFilter, message: str) -> bool:
    """筛选值被员工原话原样引用，也算有依据。

    校验原先只认「字段语义片段」——原话里要出现"颜色""材质"这类词。于是这些提问
    会被判成"筛选条件缺少当前问题依据"，而值明明是原话里的：

        「镜面玻璃·镜面 3 款是哪三款」   ← 承接上一轮的统计结果，不会再写"颜色"
        「灰玻璃·枪灰产品有哪些」

    值被原样引用同样是依据，而且不放松安全性：值必须出现在**当前这条消息**里，
    模型仍然编不出员工没说过的筛选值。

    只对文本值生效：数字太容易误命中（"12 种"里的 12），数值筛选仍由字段语义片段把关。
    """
    value = item.value
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_text_value_is_quoted(part, message) for part in value)
    return _text_value_is_quoted(value, message)


def _text_value_is_quoted(value: object, message: str) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return False
    text = _normalized(str(value))
    return bool(text) and text in _normalized(message)


def _filter_value_is_valid(item: ProductFilter, value_type: str) -> bool:
    if item.operator in {"is_present", "is_empty"}:
        return True
    values = item.value if isinstance(item.value, (list, tuple)) else (item.value,)
    if item.operator == "between" and len(values) != 2:
        return False
    if item.operator == "in" and not values:
        return False
    if value_type == "number":
        try:
            for value in values:
                float(value)
        except (TypeError, ValueError):
            return False
        return True
    return all(isinstance(value, str) and bool(value.strip()) for value in values)


def detect_product_ambiguities(message: str) -> tuple[ProductAmbiguity, ...]:
    if re.search(
        r"(?:成本|采购|进价).{0,8}(?:还是|或|或者).{0,8}(?:价格|售价|套装价|主体价)"
        r"|(?:价格|售价|套装价|主体价).{0,8}(?:还是|或|或者).{0,8}(?:成本|采购|进价)",
        message,
    ):
        return (
            ProductAmbiguity(
                code="price_scope",
                question="你想查询公开销售价格，还是成本/采购价格？",
                options=("销售价格", "成本价格"),
            ),
        )
    if re.search(
        r"(?:主体|主体价).{0,10}(?:还是|或|或者).{0,10}(?:套装|套装价|公开价格)"
        r"|(?:套装|套装价|公开价格).{0,10}(?:还是|或|或者).{0,10}(?:主体|主体价)",
        message,
    ):
        return (
            ProductAmbiguity(
                code="price_scope",
                question="你想查询主体价格，还是整套产品的套装价格？",
                options=("主体价格", "套装价格"),
            ),
        )
    if any(
        re.search(r"套装.{0,3}(?:材料|材质|用料)", clause)
        and not any(word in clause for word in ("配件", "清单", "主体", "本体"))
        for clause in _positive_clauses(message)
    ):
        return (
            ProductAmbiguity(
                code="set_material_or_components",
                question="你想了解产品主体的材质，还是套装包含的配件？",
                options=("材质", "配件"),
            ),
        )
    return ()


def fallback_field_requests(message: str) -> tuple[ProductFieldRequest, ...]:
    requests: list[ProductFieldRequest] = []
    for field_name in PRODUCT_FIELD_CATALOG:
        spans = field_semantic_spans(field_name, message)
        if spans:
            requests.append(ProductFieldRequest(field_name, max(spans, key=len), "fallback"))
    return tuple(dict.fromkeys(requests))


@dataclass(frozen=True)
class ProductOperationDefinition:
    """一个产品操作的含义与识别模式；模式为空表示当前不做确定性识别。"""

    label: str
    description: str
    patterns: tuple[str, ...] = ()


# 操作本体。加一种问法应当改这张表，而不是在规划层再加一个 `if marker in query`。
PRODUCT_OPERATION_CATALOG: dict[ProductOperation, ProductOperationDefinition] = {
    "count": ProductOperationDefinition(
        "数量统计",
        "统计匹配产品的数量；不要求指定维度或字段",
        patterns=(
            r"(?:有多少|多少种|几种|有几个|几个|产品总数|产品数量).{0,8}(?:产品|型号|款|种)",
            r"(?:产品|型号|款).{0,8}(?:有多少|多少种|几种|有几个|几个)",
            r"(?:产品|型号|款)的?(?:总数|数量)",
        ),
    ),
    "compare": ProductOperationDefinition(
        "对比",
        "比较两个或多个已登记型号的字段取值",
        patterns=(
            r"(?:哪个|哪款|哪一个|哪种).{0,4}(?:贵|便宜|划算|好|合适)",
            r"对比|比较|区别|差别|差异|相差",
        ),
    ),
    "recommend": ProductOperationDefinition(
        "推荐",
        "按员工给出的条件推荐产品",
        patterns=(r"推荐|适合|哪款好|哪个好",),
    ),
    "list": ProductOperationDefinition("清单", "盘点产品库或某一类产品"),
    "detail": ProductOperationDefinition("详情", "查询指定型号的字段"),
    "search": ProductOperationDefinition("检索", "按关键词检索产品"),
    "media": ProductOperationDefinition("媒体", "盘点或发送产品图片与视频"),
}

# 判定顺序：越具体的操作越先匹配，detail 是兜底因此不参与识别。
_OPERATION_MATCH_ORDER: tuple[ProductOperation, ...] = ("count", "compare", "recommend")


def _is_unscoped_product_overview(message: str) -> bool:
    """识别没有可执行条件的产品概览问句。"""
    normalized = _normalized(message).strip()
    return bool(
        re.fullmatch(
            r"(?:现在|当前|目前|我们(?:现在)?|你们(?:现在)?)?"
            r"(?:都|一共|总共)?"
            r"(?:有什么|有哪些|有哪几种|有哪几款|有多少(?:种|款|个)?|"
            r"有几种|有几款|有几个)"
            r"(?:产品|型号|款|种|个)(?:呀|啊|呢|吗|嘛|吧)?",
            normalized,
        )
    )


def detect_product_operation(query: str) -> ProductOperation | None:
    """按操作本体判定比「详情」更具体的操作；无法确定时返回 None。

    返回 None 表示本函数不替调用方决定兜底操作，由调用方按清单规则与会话上下文决定。
    """
    normalized = _normalized(query)
    # “有几种颜色/材质/档位” is a distinct-value aggregate, not an
    # entity count.  Keep this check before the broad count patterns because
    # both forms contain “几种”.  The field ontology is the source of truth;
    # arbitrary words must not turn a request into an executable aggregate.
    distinct_markers = r"(?:有多少种|多少种|有几种|几种|有多少类|多少类)"
    if re.search(distinct_markers, normalized):
        # Require the dimension label to be adjacent to the distinct-count
        # phrase.  A price predicate such as “100 到 200 之间有多少产品” also
        # contains an aggregatable price field, but it is an entity count and
        # must remain ``count``.
        for _field_name, definition in PRODUCT_FIELD_CATALOG.items():
            if not definition.aggregatable:
                continue
            aliases = "|".join(re.escape(alias) for alias in definition.aliases)
            if re.search(
                rf"(?:{aliases}).{{0,6}}{distinct_markers}|"
                rf"{distinct_markers}.{{0,6}}(?:{aliases})",
                normalized,
            ):
                return "aggregate"
    for operation in _OPERATION_MATCH_ORDER:
        definition = PRODUCT_OPERATION_CATALOG[operation]
        if any(re.search(pattern, normalized) for pattern in definition.patterns):
            return operation
    return None


_PREPARSE_TEXT_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (name, definition.aliases)
    for name, definition in PRODUCT_FIELD_CATALOG.items()
    if definition.filterable
    and definition.value_type == "text"
    and definition.role in {"identifier", "dimension"}
    and not definition.sensitive
    and name not in {"model", "special_function"}
)


def _preparse_value(value: str) -> str:
    value = re.split(
        r"(?:的)?(?:产品|型号|款|有)?(?:哪些|有多少|多少种|几款|多少款|是哪|分别)",
        value,
        maxsplit=1,
    )[0]
    value = value.strip(" \t，,；;。！？!?：:")
    for suffix in ("的有", "有", "的"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.strip()


def _preparse_text_filters(message: str) -> tuple[ProductFilter, ...]:
    """Extract explicit text conditions using the shared field catalogue."""
    compact = message.strip()
    found: list[ProductFilter] = []
    stop_words = (
        "并且", "以及", "同时", "另外", "且", "和", "与", "、",
        "产品", "型号", "款", "哪些", "有多少", "多少种", "几款",
    )
    stop_pattern = "|".join(re.escape(word) for word in sorted(stop_words, key=len, reverse=True))
    for field, aliases in _PREPARSE_TEXT_FIELDS:
        if not field_semantic_spans(field, message):
            continue
        alias_pattern = "|".join(
            re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)
        )
        after = re.compile(
            rf"(?P<label>{alias_pattern})\s*(?:是|为|叫|：|:|等于|包含|包括|在)?\s*"
            rf"(?P<value>[\u3400-\u9fffA-Za-z0-9·_.+\-/％%]+?)(?={stop_pattern}|$)"
        )
        for match in after.finditer(compact):
            value = _preparse_value(match.group("value"))
            if (
                not value
                or value in aliases
                or value
                in {
                    "什么", "哪些", "有", "的", "呢", "吗", "嘛", "吧",
                    *REFERENCE_PRONOUN_SET,
                }
                or any(
                    alias in value
                    for _, field_aliases in _PREPARSE_TEXT_FIELDS
                    for alias in field_aliases
                    if alias not in aliases
                )
            ):
                continue
            if value.startswith(("什么", "哪些", "哪种", "哪款")):
                continue
            evidence = compact[max(0, match.start()) : match.end()]
            operator = (
                "contains" if any(word in evidence for word in ("包含", "包括")) else "equals"
            )
            found.append(ProductFilter(field, operator, value, evidence, "fallback"))

        before = re.compile(
            rf"(?:^|[，,；;。！？!?、])(?P<value>[\u3400-\u9fffA-Za-z0-9·_.+\-/％%]{{1,32}})\s*(?:的)?"
            rf"(?P<label>{alias_pattern})"
        )
        for match in before.finditer(compact):
            value = _preparse_value(match.group("value"))
            if (
                not value
                or value in aliases
                or value.endswith(("主体", "产品", "套装"))
                or value.startswith(("支持", "带有", "带", "具有", "含有", "有"))
                or any(
                    alias in value
                    for _, field_aliases in _PREPARSE_TEXT_FIELDS
                    for alias in field_aliases
                )
                or re.search(r"什么|哪些|哪种|哪款", value)
                or re.search(r"[A-Za-z]+\d", value)
                or value in {*REFERENCE_PRONOUN_SET, "呢", "吗", "嘛"}
            ):
                continue
            if field == "series" and any(
                token.casefold() == value.casefold()
                for token in re.findall(r"[A-Za-z]+\d+", value)
            ):
                continue
            evidence = compact[max(0, match.start()) : match.end()]
            found.append(ProductFilter(field, "contains", value, evidence, "fallback"))

    generic = re.search(r"(?:有|含有|带有|具有|支持)\s*特殊功能", compact)
    if generic:
        found.append(
            ProductFilter("special_function", "is_present", "", generic.group(), "fallback")
        )
    elif not _is_unscoped_product_overview(compact):
        for match in re.finditer(
            r"(?:支持|带有|带|具有|含有|有)\s*"
            r"(?P<value>[\u3400-\u9fffA-Za-z0-9·_.+\-/]{1,24}?)"
            r"(?:功能)?(?=的产品|产品|且|并且|同时|以及|和|与|[，,；;。]|$)",
            compact,
        ):
            value = _preparse_value(match.group("value"))
            if value and value != "特殊" and not value.startswith(
                ("什么", "多少", "几个", "哪些", "哪")
            ):
                found.append(
                    ProductFilter("special_function", "contains", value, match.group(), "fallback")
                )
    unique: dict[tuple[object, ...], ProductFilter] = {}
    for item in found:
        unique.setdefault((item.field, item.operator, item.value), item)
    return tuple(unique.values())


def _preparse_numeric_filters(message: str) -> tuple[ProductFilter, ...]:
    """Extract explicit numeric/range constraints for prices and quantities."""
    number = r"(?:￥|¥)?\d+(?:\.\d+)?"
    relation = (
        r"以内|以下|之内|不超过|最多|不高于|以上|不低于|不少于|起步|低于|少于|小于|"
        r"不到|高于|超过|大于|左右|上下"
    )
    patterns = (
        re.compile(rf"(?P<left>{number})\s*(?:到|至|~|～|—|-)\s*(?P<right>{number})"),
        re.compile(rf"(?P<num>{number})\s*(?P<rel>{relation})"),
        re.compile(rf"(?P<rel>低于|少于|小于|不到|高于|超过|大于|不超过|不低于|不少于)\s*(?P<num>{number})"),
    )
    filters: list[ProductFilter] = []
    consumed: set[tuple[int, int]] = set()
    for pattern in patterns:
        for match in pattern.finditer(message):
            if any(
                start <= match.start() < end or start < match.end() <= end
                for start, end in consumed
            ):
                continue
            prefix = message[max(0, match.start() - 18) : match.start()]
            # A second condition after “且/并且/以及/，” has its own field
            # context; do not let the preceding price cue bleed into it.
            prefix = re.split(r"[，,；;。！？!?、]|并且|以及|同时|且|和|与", prefix)[-1]
            semantic_evidence = prefix + match.group()
            numeric_fields = [
                field
                for field, definition in PRODUCT_FIELD_CATALOG.items()
                if definition.filterable
                and definition.value_type == "number"
                and _field_supported_by_evidence(field, semantic_evidence)
            ]
            if len(numeric_fields) != 1:
                continue
            field = numeric_fields[0]
            if "left" in match.groupdict():
                value: object = (float(match.group("left")), float(match.group("right")))
                operator = "between"
            else:
                numeric = float(match.group("num"))
                rel = match.group("rel")
                if rel in {"以内", "以下", "之内", "不超过", "最多", "不高于"}:
                    operator = "less_than_or_equal"
                    value = numeric
                elif rel in {"以上", "不低于", "不少于", "起步"}:
                    operator = "greater_than_or_equal"
                    value = numeric
                elif rel in {"低于", "少于", "小于", "不到"}:
                    operator = "less_than"
                    value = numeric
                elif rel in {"高于", "超过", "大于"}:
                    operator = "greater_than"
                    value = numeric
                else:
                    operator = "between"
                    value = (numeric * 0.9, numeric * 1.1)
            evidence = message[max(0, match.start() - 12) : match.end()].strip()
            filters.append(ProductFilter(field, operator, value, evidence, "fallback"))
            consumed.add((match.start(), match.end()))
    return tuple(filters)


def _preparse_sort_and_page(message: str) -> tuple[tuple[ProductSort, ...], int, int]:
    sorts: list[ProductSort] = []
    label = r"成本价|采购价|进价|主体价|主体价格|套装价|价格"
    descending = re.search(
        rf"(?P<label>{label}).{{0,4}}(?:从高到低|降序|由高到低|最高优先|贵到便宜)",
        message,
    )
    ascending = re.search(
        rf"(?P<label>{label}).{{0,4}}(?:从低到高|升序|由低到高|便宜到贵)",
        message,
    )
    matched = descending or ascending
    if matched:
        price_label = matched.group("label")
        field = (
            "cost_price"
            if price_label in {"成本价", "采购价", "进价"}
            else "body_price"
            if price_label in {"主体价", "主体价格"}
            else "set_price"
        )
        sorts.append(ProductSort(field, "desc" if descending else "asc"))
    page = 1
    page_size = 5
    page_match = re.search(r"第\s*(\d+)\s*页", message)
    if page_match:
        page = max(1, int(page_match.group(1)))
    size_match = re.search(r"每页\s*(\d+)\s*(?:个|款|条)?", message)
    if size_match:
        page_size = max(1, min(5, int(size_match.group(1))))
    return tuple(sorts), page, page_size


def preparse_product_query(message: str) -> ProductQuery:
    """Deterministically pre-parse explicit employee conditions."""
    from app.business.product_selection import product_identifier_tokens

    tokens = tuple(product_identifier_tokens(message))
    references = tuple(ProductReference(token, "fragment", token, "fallback") for token in tokens)
    filters = (*_preparse_text_filters(message), *_preparse_numeric_filters(message))
    sorts, page, page_size = _preparse_sort_and_page(message)
    operation = detect_product_operation(message)
    aggregate = None
    # “最便宜/最低价的产品价格是多少”是全库最低价统计，不是缺少型号的详情查询。
    if re.search(r"(?:最便宜|最低价|价格最低|最划算)", message) and re.search(
        r"(?:价格|价钱|多少钱|售价|套装价|主体价)", message
    ):
        operation = "aggregate"
        aggregate = ProductAggregate(
            kind="min",
            field="body_price" if re.search(r"主体价|主体价格", message) else "set_price",
            evidence="主体价" if re.search(r"主体价|主体价格", message) else "价格",
            source="fallback",
        )
    if operation == "aggregate" and aggregate is None:
        # Select the first explicitly named aggregatable field.  This is only
        # a deterministic candidate; validate_product_query still checks
        # evidence and permissions before execution.
        aggregate_fields = [
            (field_name, request)
            for request in fallback_field_requests(message)
            for field_name in (request.field,)
            if PRODUCT_FIELD_CATALOG[field_name].aggregatable
        ]
        if aggregate_fields:
            field_name, request = aggregate_fields[0]
            aggregate = ProductAggregate(
                kind="distinct_count",
                field=field_name,
                evidence=request.evidence,
                source="fallback",
            )
    if operation is None and filters:
        # A numeric/text predicate describes a candidate set.  Treat it as a
        # listing even when the employee says “发我…产品” instead of “有哪些”;
        # otherwise a detail plan can discard the predicate and ask for a field.
        operation = "list"
    if operation is None and re.search(r"(?:列出|有什么|有哪些|哪几款|哪些产品)", message):
        operation = "list"
    # Entity counts do not have a field/value predicate.  The broad text
    # pre-parser can otherwise mistake the tail of “有几种产品” for a
    # special_function value and make a valid count unsafe.
    if operation == "count" and aggregate is None:
        # Keep real predicates such as “100 到 200 元内有多少产品”; only drop
        # the false special_function match produced by the broad text parser.
        filters = tuple(
            item
            for item in filters
            if not (
                item.field == "special_function"
                and isinstance(item.value, str)
                and "产品" in item.value
            )
        )
    return ProductQuery(
        operation=operation or "detail",
        include_models=tuple(item.value for item in references),
        references=references,
        filters=tuple(filters),
        sorts=sorts,
        aggregate=aggregate,
        page=page,
        page_size=page_size,
        required_references=references,
        required_filters=tuple(filters),
        required_operation=(
            operation
            if operation != "list" or _is_unscoped_product_overview(message) or bool(filters)
            else None
        ),
    )


deterministic_product_preparse = preparse_product_query


def _same_filter(left: ProductFilter, right: ProductFilter) -> bool:
    def canonical(value: object) -> object:
        if isinstance(value, (tuple, list)):
            return tuple(canonical(item) for item in value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value.casefold().replace(" ", "") if isinstance(value, str) else value

    return (
        left.field == right.field
        and left.operator == right.operator
        and canonical(left.value) == canonical(right.value)
    )


def validate_product_query(
    product_query: ProductQuery,
    message: str,
    *,
    can_view_cost: bool,
    trusted_context: ProductQuery | None = None,
    trusted_focus: ProductFocus | None = None,
) -> ProductValidationResult:
    """Ground model output in the employee's words and enforce field permissions."""
    errors: list[str] = []
    if product_query.target != "product":
        errors.append("count 只支持 target=product")
    # The deterministic pre-parser is a safety boundary, not merely a hint.
    # Every explicit condition must survive model assembly; otherwise a model
    # that drops a price range could execute a broad query.
    preparse = preparse_product_query(message)
    # A continuation like “的详细信息” intentionally asks for the trusted
    # product overview.  The planner may still carry the previous turn's
    # field list as context evidence; validating those fields one by one
    # causes a false “另有字段未能可靠确认” warning even though the detail
    # renderer is already authorized to emit the complete record.
    contextual_overview = (
        product_query.operation == "detail"
        and product_query.context_mode == "continuation"
        and is_overview_phrase(message)
    )
    if contextual_overview:
        product_query = replace(
            product_query,
            requested_fields=(),
            fields=(),
            original_fields=(),
            allow_overview=True,
        )
    required_operation = product_query.required_operation or (
        preparse.required_operation
        if preparse.required_operation
        in {"count", "compare", "recommend", "aggregate", "list"}
        else None
    )
    required_references = product_query.required_references or preparse.required_references
    required_filters = product_query.required_filters or preparse.required_filters
    if required_operation and product_query.operation != required_operation:
        errors.append(
            f"产品操作与原话不一致：原话要求 {required_operation}，"
            f"模型给出 {product_query.operation}"
        )
    proposed_references = product_query.effective_references()
    for required in required_references:
        if not any(
            _normalized(required.value) == _normalized(item.value)
            for item in proposed_references
        ):
            errors.append(f"型号条件未被保留：{required.value}")
    for required in required_filters:
        if not any(_same_filter(required, item) for item in product_query.filters):
            errors.append(f"产品筛选条件未被保留：{required.field}")
    proposed = {item.field for item in product_query.effective_field_requests()}
    removed: list[str] = [name for name in product_query.original_fields if name not in proposed]
    denied: list[str] = []
    context = trusted_context if product_query.context_mode == "continuation" else None
    context_fields = (
        {item.field for item in context.effective_field_requests()} if context else set()
    )
    current_fields = {item.field for item in fallback_field_requests(message)}
    if current_fields:
        context_fields &= current_fields
    context_references = (
        {item.value.casefold() for item in context.effective_references()} if context else set()
    )
    ambiguities = tuple(
        dict.fromkeys((*detect_product_ambiguities(message), *product_query.ambiguities))
    )
    if (
        trusted_focus is not None
        and not trusted_focus.is_valid()
        and product_query.context_mode == "continuation"
    ):
        errors.append("产品上下文已过期，请重新列出产品后再按序号查询")

    references: list[ProductReference] = []
    resolved_ids: list[str] = list(product_query.resolved_product_ids)
    if trusted_focus is not None and (trusted_focus.result_ids or trusted_focus.trusted_ids):
        allowed_ids = {*trusted_focus.result_ids, *trusted_focus.trusted_ids}
        if any(item not in allowed_ids for item in resolved_ids):
            errors.append("上下文产品不在上一轮展示结果中")
            resolved_ids = []
    for reference in product_query.effective_references():
        if not reference.value.strip():
            continue
        focus_id = None
        if reference.source == "context" and trusted_focus is not None and (
            trusted_focus.result_ids or trusted_focus.trusted_ids
        ):
            allowed_ids = {*trusted_focus.result_ids, *trusted_focus.trusted_ids}
            if reference.value.startswith("id:"):
                candidate_id = reference.value.removeprefix("id:")
                if candidate_id in allowed_ids:
                    focus_id = candidate_id
            else:
                focus_id = trusted_focus.model_to_id(reference.value)
            grounded = focus_id is not None
        else:
            grounded = (
                reference.value.casefold() in context_references
                if reference.source == "context"
                else _evidence_is_grounded(
                    reference.evidence
                    or (reference.value if reference.source == "fallback" else ""),
                    message,
                )
                and _normalized(reference.value) in _normalized(message)
            )
        grounded = grounded and reference.source in {"current", "fallback", "context"}
        if not grounded:
            errors.append(f"型号引用缺少当前问题依据：{reference.value}")
            continue
        if focus_id is not None:
            resolved_ids.append(focus_id)
            references.append(replace(reference, value=f"id:{focus_id}", kind="context"))
        else:
            references.append(reference)

    requests: list[ProductFieldRequest] = []
    for request in product_query.effective_field_requests():
        if request.field not in PRODUCT_FIELD_CATALOG:
            errors.append(f"不支持的产品字段：{request.field}")
            continue
        definition = PRODUCT_FIELD_CATALOG[request.field]
        grounded = (
            request.field in context_fields
            if request.source == "context"
            else field_evidence_is_valid(request.field, request.evidence, message)
            if request.evidence
            else request.source == "fallback"
            and _field_supported_by_evidence(request.field, message)
        )
        grounded = grounded and request.source in {"current", "fallback", "context"}
        if not grounded:
            removed.append(request.field)
            continue
        if definition.sensitive and not can_view_cost:
            denied.append(request.field)
            continue
        requests.append(request)

    filters: list[ProductFilter] = []
    for item in product_query.filters:
        definition = PRODUCT_FIELD_CATALOG.get(item.field)
        if definition is None or not definition.filterable:
            errors.append(f"不支持的筛选字段：{item.field}")
            continue
        allowed = _NUMBER_OPERATORS if definition.value_type == "number" else _TEXT_OPERATORS
        if item.operator not in allowed:
            errors.append(f"字段 {item.field} 不支持操作符 {item.operator}")
            continue
        grounded = (
            context is not None
            and any(
                (previous.field, previous.operator, previous.value)
                == (item.field, item.operator, item.value)
                for previous in context.filters
            )
            if item.source == "context"
            else field_evidence_is_valid(
                item.field, item.evidence or (message if item.source == "fallback" else ""), message
            )
            # 「镜面玻璃·镜面 3 款是哪三款」这类承接提问不会重复说"颜色"，
            # 只要筛选值被原话原样引用，就不该判成无依据（见 _filter_value_is_quoted）。
            # 但**依据为空**仍然拦下：那是模型连引用都没给，属于残缺计划，
            # 不能靠"值恰好出现在原话里"蒙过去（tests/test_product_query_semantics.py 锁住这条）。
            or (
                bool(item.evidence.strip())
                and _filter_value_is_quoted(item, message)
            )
        )
        grounded = grounded and item.source in {"current", "fallback", "context"}
        if not grounded:
            errors.append(f"筛选条件缺少当前问题依据：{item.field}")
            continue
        if not _filter_value_is_valid(item, definition.value_type):
            errors.append(f"筛选条件的值无效：{item.field}")
            continue
        if definition.sensitive and not can_view_cost:
            denied.append(item.field)
            continue
        filters.append(item)

    sorts: tuple[ProductSort, ...] = ()
    accepted_sorts: list[ProductSort] = []
    for item in product_query.sorts:
        definition = PRODUCT_FIELD_CATALOG.get(item.field)
        if definition is None or not definition.can_sort:
            errors.append(f"不支持的排序字段：{item.field}")
            continue
        if definition.sensitive and not can_view_cost:
            denied.append(item.field)
            continue
        accepted_sorts.append(item)
    sorts = tuple(accepted_sorts[:3])

    # 聚合：与字段/筛选同一套标准——字段存在、原话里有依据、没被权限挡下；
    # max/min/average 还要求字段是数值（对文本求平均没有意义）。
    # 任何一条不过就丢掉聚合，让执行层去澄清，而不是拿一个没依据的字段去算。
    aggregate = product_query.aggregate
    if product_query.operation == "aggregate" or aggregate is not None:
        if aggregate is None:
            errors.append("聚合任务缺少 aggregate")
        else:
            definition = PRODUCT_FIELD_CATALOG.get(aggregate.field)
            if definition is None or definition.value_type in {"media", "json"}:
                errors.append(f"不支持的聚合字段：{aggregate.field}")
                aggregate = None
            elif aggregate.kind in {"max", "min", "average"} and definition.value_type != "number":
                errors.append(f"该字段不能做 {aggregate.kind}：{aggregate.field}")
                aggregate = None
            elif definition.sensitive and not can_view_cost:
                denied.append(aggregate.field)
                aggregate = None
            else:
                grounded = (
                    aggregate.field in context_fields
                    if aggregate.source == "context"
                    else field_evidence_is_valid(aggregate.field, aggregate.evidence, message)
                    if aggregate.evidence
                    else _field_supported_by_evidence(aggregate.field, message)
                )
                if not (grounded and aggregate.source in {"current", "fallback", "context"}):
                    removed.append(aggregate.field)
                    aggregate = None

    validated = replace(
        product_query,
        references=tuple(references),
        include_models=tuple(reference.value for reference in references),
        requested_fields=tuple(dict.fromkeys(requests)),
        fields=tuple(dict.fromkeys(request.field for request in requests)),
        filters=tuple(filters),
        sorts=sorts,
        aggregate=aggregate,
        page=max(1, product_query.page),
        page_size=max(
            1,
            min(10 if product_query.operation == "recommend" else 5, product_query.page_size),
        ),
        ambiguities=ambiguities,
        allow_overview=product_query.allow_overview
        or is_overview_request(message, tuple(references))
        or bool(
            context
            and context.allow_overview
            and message.strip() in {"下一页", "上一页", "下页", "上页", "继续看"}
        ),
        original_fields=tuple(
            dict.fromkeys(
                (
                    *product_query.original_fields,
                    *(item.field for item in product_query.effective_field_requests()),
                    *(item.field for item in fallback_field_requests(message)),
                )
            )
        ),
        resolved_product_ids=tuple(dict.fromkeys(resolved_ids)),
    )
    return ProductValidationResult(
        query=validated,
        errors=tuple(errors),
        removed_fields=tuple(dict.fromkeys(removed)),
        permission_denied_fields=tuple(dict.fromkeys(denied)),
        ambiguities=ambiguities,
    )


def product_query_to_dict(product_query: ProductQuery) -> dict[str, object]:
    """Return a JSON-safe plan for context and audit storage (never query results)."""
    return {
        "version": product_query.version,
        "operation": product_query.operation,
        "target": product_query.target,
        "include_models": list(product_query.include_models),
        "exclude_models": list(product_query.exclude_models),
        "fields": list(product_query.fields),
        "original_fields": list(product_query.original_fields),
        "allow_overview": product_query.allow_overview,
        "references": [
            {
                "value": item.value,
                "kind": item.kind,
                "evidence": item.evidence,
                "source": item.source,
            }
            for item in product_query.effective_references()
        ],
        "requested_fields": [
            {"field": item.field, "evidence": item.evidence, "source": item.source}
            for item in product_query.effective_field_requests()
        ],
        "filters": [
            {
                "field": item.field,
                "operator": item.operator,
                "value": item.value,
                "evidence": item.evidence,
                "source": item.source,
            }
            for item in product_query.filters
        ],
        "result_scope": product_query.result_scope,
        "sorts": [
            {"field": item.field, "direction": item.direction} for item in product_query.sorts
        ],
        "aggregate": (
            {
                "kind": product_query.aggregate.kind,
                "field": product_query.aggregate.field,
                "limit": product_query.aggregate.limit,
                "evidence": product_query.aggregate.evidence,
                "source": product_query.aggregate.source,
            }
            if product_query.aggregate is not None
            else None
        ),
        "page": product_query.page,
        "page_size": product_query.page_size,
        "context_mode": product_query.context_mode,
        "required_references": [
            {
                "value": item.value,
                "kind": item.kind,
                "evidence": item.evidence,
                "source": item.source,
            }
            for item in product_query.required_references
        ],
        "required_filters": [
            {
                "field": item.field,
                "operator": item.operator,
                "value": item.value,
                "evidence": item.evidence,
                "source": item.source,
            }
            for item in product_query.required_filters
        ],
        "required_operation": product_query.required_operation,
        "resolved_product_ids": list(product_query.resolved_product_ids),
        "ambiguities": [
            {"code": item.code, "question": item.question, "options": list(item.options)}
            for item in product_query.ambiguities
        ],
    }


def product_query_from_dict(value: object) -> ProductQuery | None:
    """Restore only context written by :func:`product_query_to_dict`."""
    if not isinstance(value, dict):
        return None
    try:
        references = tuple(
            ProductReference(
                value=str(item["value"]),
                kind=str(item.get("kind") or "fragment"),  # type: ignore[arg-type]
                evidence=str(item.get("evidence") or ""),
                source=str(item.get("source") or "context"),  # type: ignore[arg-type]
            )
            for item in value.get("references", [])
            if isinstance(item, dict) and item.get("value")
        )
        requests = tuple(
            ProductFieldRequest(
                field=str(item["field"]),
                evidence=str(item.get("evidence") or ""),
                source=str(item.get("source") or "context"),  # type: ignore[arg-type]
            )
            for item in value.get("requested_fields", [])
            if isinstance(item, dict) and item.get("field")
        )
        filters = tuple(
            ProductFilter(
                field=str(item["field"]),
                operator=str(item["operator"]),  # type: ignore[arg-type]
                value=item.get("value", ""),
                evidence=str(item.get("evidence") or ""),
                source=str(item.get("source") or "context"),  # type: ignore[arg-type]
            )
            for item in value.get("filters", [])
            if isinstance(item, dict) and item.get("field") and item.get("operator")
        )
        required_references = tuple(
            ProductReference(
                value=str(item["value"]),
                kind=str(item.get("kind") or "fragment"),  # type: ignore[arg-type]
                evidence=str(item.get("evidence") or ""),
                source=str(item.get("source") or "fallback"),  # type: ignore[arg-type]
            )
            for item in value.get("required_references", [])
            if isinstance(item, dict) and item.get("value")
        )
        required_filters = tuple(
            ProductFilter(
                field=str(item["field"]),
                operator=str(item.get("operator") or "contains"),  # type: ignore[arg-type]
                value=item.get("value", ""),
                evidence=str(item.get("evidence") or ""),
                source=str(item.get("source") or "fallback"),  # type: ignore[arg-type]
            )
            for item in value.get("required_filters", [])
            if isinstance(item, dict) and item.get("field")
        )
        sorts = tuple(
            ProductSort(
                field=str(item["field"]),
                direction=str(item.get("direction") or "asc"),  # type: ignore[arg-type]
            )
            for item in value.get("sorts", [])
            if isinstance(item, dict) and item.get("field")
        )
        raw_aggregate = value.get("aggregate")
        aggregate: ProductAggregate | None = None
        if isinstance(raw_aggregate, dict) and raw_aggregate.get("field"):
            kind = str(raw_aggregate.get("kind") or "distinct_count")
            if kind in {"distinct_count", "max", "min", "average"}:
                aggregate = ProductAggregate(
                    kind=kind,  # type: ignore[arg-type]
                    field=str(raw_aggregate["field"]),
                    limit=max(1, min(50, int(raw_aggregate.get("limit") or 30))),
                    evidence=str(raw_aggregate.get("evidence") or ""),
                    source=str(raw_aggregate.get("source") or "context"),  # type: ignore[arg-type]
                )
        target = str(value.get("target") or "product")
        if target != "product":
            return None
        return ProductQuery(
            operation=str(value.get("operation") or "detail"),  # type: ignore[arg-type]
            target=target,  # type: ignore[arg-type]
            include_models=tuple(str(item) for item in value.get("include_models", [])),
            exclude_models=tuple(str(item) for item in value.get("exclude_models", [])),
            fields=tuple(str(item) for item in value.get("fields", [])),
            original_fields=tuple(str(item) for item in value.get("original_fields", [])),
            allow_overview=value.get("allow_overview") is True,
            filters=filters,
            version=int(value.get("version") or 2),
            references=references,
            requested_fields=requests,
            result_scope=str(value.get("result_scope") or "all_matches"),  # type: ignore[arg-type]
            sorts=sorts,
            aggregate=aggregate,
            page=max(1, int(value.get("page") or 1)),
            page_size=max(1, int(value.get("page_size") or 5)),
            context_mode=str(value.get("context_mode") or "continuation"),  # type: ignore[arg-type]
            required_references=required_references,
            required_filters=required_filters,
            required_operation=(
                str(value.get("required_operation"))  # type: ignore[arg-type]
                if value.get("required_operation")
                else None
            ),
            resolved_product_ids=tuple(
                str(item) for item in value.get("resolved_product_ids", []) if str(item).strip()
            ),
        )
    except (TypeError, ValueError):
        return None
