"""Validated, model-facing product query contract.

字段目录的**唯一权威**已经搬到 `app.ontology.schema`（那里多了"领域角色"：
标识符 / 维度 / 度量 / 文本 / 媒体）。这里重新导出，供既有导入点继续使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

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
    "detail", "search", "list", "compare", "recommend", "media", "aggregate"
]
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
# 字段」：旧链路会列出全部字段，V2 直接拒答。这正是同类词表漏项那一类缺陷。
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
    "一下",
    "请",
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
    remainder = remainder.strip("。！？!?，,：:")
    return _OVERVIEW_FILLER_PATTERN.fullmatch(remainder) is not None


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
_OPERATION_MATCH_ORDER: tuple[ProductOperation, ...] = ("compare", "recommend")


def detect_product_operation(query: str) -> ProductOperation | None:
    """按操作本体判定比「详情」更具体的操作；无法确定时返回 None。

    返回 None 表示本函数不替调用方决定兜底操作，由调用方按清单规则与会话上下文决定。
    """
    normalized = _normalized(query)
    for operation in _OPERATION_MATCH_ORDER:
        definition = PRODUCT_OPERATION_CATALOG[operation]
        if any(re.search(pattern, normalized) for pattern in definition.patterns):
            return operation
    return None



def validate_product_query(
    product_query: ProductQuery,
    message: str,
    *,
    can_view_cost: bool,
    trusted_context: ProductQuery | None = None,
) -> ProductValidationResult:
    """Ground model output in the employee's words and enforce field permissions."""
    errors: list[str] = []
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

    references: list[ProductReference] = []
    for reference in product_query.effective_references():
        if not reference.value.strip():
            continue
        grounded = (
            reference.value.casefold() in context_references
            if reference.source == "context"
            else _evidence_is_grounded(
                reference.evidence or (reference.value if reference.source == "fallback" else ""),
                message,
            )
            and _normalized(reference.value) in _normalized(message)
        )
        grounded = grounded and reference.source in {"current", "fallback", "context"}
        if not grounded:
            errors.append(f"型号引用缺少当前问题依据：{reference.value}")
            continue
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
        if definition is None or definition.value_type in {"media", "json"}:
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
        allow_overview=is_overview_request(message, tuple(references))
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
        return ProductQuery(
            operation=str(value.get("operation") or "detail"),  # type: ignore[arg-type]
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
        )
    except (TypeError, ValueError):
        return None
