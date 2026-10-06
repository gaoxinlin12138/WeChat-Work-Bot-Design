"""Validated, model-facing product query contract and business field ontology."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

ProductOperation = Literal["detail", "search", "list", "compare", "recommend", "media"]
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


PRODUCT_FIELD_CATALOG: dict[str, ProductFieldDefinition] = {
    "model": ProductFieldDefinition(
        "产品型号",
        "text",
        "产品的正式完整型号",
        ("型号",),
        patterns=(r"(?:哪些|所有|全部|列出).{0,6}产品",),
    ),
    "series": ProductFieldDefinition("系列", "text", "产品所属系列", ("系列",)),
    "description": ProductFieldDefinition(
        "产品描述", "text", "产品公开描述", ("产品描述", "描述", "介绍")
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
    ),
    "spray_modes": ProductFieldDefinition("档位", "text", "产品登记的档位", ("档位", "几档")),
    "color": ProductFieldDefinition("颜色", "text", "产品颜色", ("颜色", "什么色", "配色")),
    "special_function": ProductFieldDefinition(
        "特殊功能",
        "text",
        "产品登记的特殊功能",
        ("特殊功能", "功能", "支持", "带有", "具有"),
        patterns=(r"(?:支持|带有|具有).{1,12}", r"(?:有|带).{1,10}(?:功能|的产品)"),
    ),
    "shipping_origin": ProductFieldDefinition(
        "出货地", "text", "产品出货地点", ("出货地", "发货地", "哪里发货")
    ),
    "set_price": ProductFieldDefinition(
        "套装价",
        "number",
        "整套产品的公开销售价格",
        ("套装价", "价格", "多少钱", "报价", "售价"),
        exclusions=(r"主体|成本|采购|进价",),
    ),
    "body_price": ProductFieldDefinition(
        "主体价",
        "number",
        "单独产品主体的公开价格",
        ("主体价", "主体多少钱"),
        patterns=(r"主体.{0,4}(?:价格|报价|售价|多少钱)",),
    ),
    "cost_price": ProductFieldDefinition(
        "成本价",
        "number",
        "内部成本价格，只有员工明确询问且通过部门权限后才可返回",
        ("成本价", "成本", "进价", "采购价"),
        sensitive=True,
    ),
    "piece_count": ProductFieldDefinition(
        "件数", "number", "当前登记件数", ("件数", "库存", "多少件", "几件")
    ),
    "supplier": ProductFieldDefinition(
        "供应商", "text", "套装供应商", ("供应商",), exclusions=(r"主体供应商",)
    ),
    "body_supplier": ProductFieldDefinition("主体供应商", "text", "主体供应商", ("主体供应商",)),
    "components": ProductFieldDefinition(
        "配件",
        "json",
        "整套产品包含的配件清单，不代表主体材质",
        ("配件", "标配", "附件", "清单"),
        patterns=(r"套装.{0,4}(?:包含|包括|有什么|有哪些|由什么组成|由哪些组成)",),
        exclusions=(r"套装.{0,6}(?:材料|材质|用料)",),
    ),
    "images": ProductFieldDefinition(
        "图片",
        "media",
        "已发布的产品图、主体图或配置图",
        ("图片", "产品图", "主体图", "配置图"),
        False,
    ),
    "videos": ProductFieldDefinition(
        "视频", "media", "已发布的产品视频", ("视频", "产品视频", "安装视频"), False
    ),
}

PRODUCT_QUERY_FIELDS = frozenset(PRODUCT_FIELD_CATALOG)
PRODUCT_FILTER_FIELDS = frozenset(
    key for key, definition in PRODUCT_FIELD_CATALOG.items() if definition.filterable
)
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


def is_overview_request(message: str, references: tuple[ProductReference, ...]) -> bool:
    """Empty fields are not an overview request; require explicit overview words or a bare model."""
    if not references:
        return False
    remainder = _normalized(message)
    for reference in references:
        remainder = remainder.replace(_normalized(reference.value), "")
    remainder = remainder.strip("。！？!?，,：:")
    return (
        re.fullmatch(
            r"(?:请|帮我|查一下|查询|查看|看看|看下|给我|提供|一下|的|这款|产品|"
            r"完整|全部|所有|详细|基本|资料|信息|详情|概览)*",
            remainder,
        )
        is not None
    )


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
    validated = replace(
        product_query,
        references=tuple(references),
        include_models=tuple(reference.value for reference in references),
        requested_fields=tuple(dict.fromkeys(requests)),
        fields=tuple(dict.fromkeys(request.field for request in requests)),
        filters=tuple(filters),
        sorts=sorts,
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
            page=max(1, int(value.get("page") or 1)),
            page_size=max(1, int(value.get("page_size") or 5)),
            context_mode=str(value.get("context_mode") or "continuation"),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        return None
