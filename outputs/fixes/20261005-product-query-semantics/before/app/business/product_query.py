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


PRODUCT_FIELD_CATALOG: dict[str, ProductFieldDefinition] = {
    "model": ProductFieldDefinition("产品型号", "text", "产品的正式完整型号", ("型号",)),
    "series": ProductFieldDefinition("系列", "text", "产品所属系列", ("系列",)),
    "description": ProductFieldDefinition(
        "产品描述", "text", "产品公开描述", ("产品描述", "描述", "介绍")
    ),
    "material": ProductFieldDefinition(
        "材质", "text", "产品主体本身使用的材质，不是套装配件", ("材质", "材料")
    ),
    "spray_modes": ProductFieldDefinition("档位", "text", "产品登记的档位", ("档位", "几档")),
    "color": ProductFieldDefinition("颜色", "text", "产品颜色", ("颜色", "色")),
    "special_function": ProductFieldDefinition(
        "特殊功能",
        "text",
        "产品登记的特殊功能",
        ("特殊功能", "功能", "支持", "带有", "具有"),
    ),
    "shipping_origin": ProductFieldDefinition(
        "出货地", "text", "产品出货地点", ("出货地", "发货地", "哪里发货")
    ),
    "set_price": ProductFieldDefinition(
        "套装价", "number", "整套产品的公开销售价格", ("套装价", "价格", "多少钱", "报价", "售价")
    ),
    "body_price": ProductFieldDefinition(
        "主体价", "number", "单独产品主体的公开价格", ("主体价", "主体多少钱")
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
    "supplier": ProductFieldDefinition("供应商", "text", "套装供应商", ("供应商",)),
    "body_supplier": ProductFieldDefinition(
        "主体供应商", "text", "主体供应商", ("主体供应商",)
    ),
    "components": ProductFieldDefinition(
        "配件",
        "json",
        "整套产品包含的配件清单，不代表主体材质",
        ("配件", "标配", "附件", "清单", "套装包含", "套装有哪些", "套装有什么"),
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
    return re.sub(r"\s+", "", value.casefold())


def _evidence_is_grounded(evidence: str, message: str) -> bool:
    return bool(evidence.strip()) and _normalized(evidence) in _normalized(message)


def _field_supported_by_evidence(field_name: str, evidence: str) -> bool:
    definition = PRODUCT_FIELD_CATALOG[field_name]
    compact = _normalized(evidence)
    if any(_normalized(alias) in compact for alias in definition.aliases):
        return True
    if field_name == "components":
        return "组成" in compact and "套装" in compact and "材料" not in compact
    if field_name == "model":
        return any(char.isdigit() for char in compact)
    return False


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
    compact = _normalized(message)
    if (
        re.search(r"套装.{0,3}(?:材料|材质)", compact)
        and not any(word in compact for word in ("配件", "包含", "清单"))
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
    compact = _normalized(message)
    requests: list[ProductFieldRequest] = []
    for field_name, definition in PRODUCT_FIELD_CATALOG.items():
        matched = next(
            (alias for alias in definition.aliases if _normalized(alias) in compact), None
        )
        if matched:
            requests.append(ProductFieldRequest(field_name, matched, "fallback"))
    return tuple(dict.fromkeys(requests))


def validate_product_query(
    product_query: ProductQuery,
    message: str,
    *,
    can_view_cost: bool,
) -> ProductValidationResult:
    """Ground model output in the employee's words and enforce field permissions."""
    errors: list[str] = []
    removed: list[str] = []
    denied: list[str] = []
    ambiguities = tuple(
        dict.fromkeys((*detect_product_ambiguities(message), *product_query.ambiguities))
    )

    references: list[ProductReference] = []
    for reference in product_query.effective_references():
        if not reference.value.strip():
            continue
        grounded = reference.source in {"context", "fallback"} or _evidence_is_grounded(
            reference.evidence, message
        )
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
        grounded = request.source in {"context", "fallback"} or (
            _evidence_is_grounded(request.evidence, message)
            and _field_supported_by_evidence(request.field, request.evidence)
        )
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
        if item.source == "current" and not (
            _evidence_is_grounded(item.evidence, message)
            and _field_supported_by_evidence(item.field, item.evidence)
        ):
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
            {"field": item.field, "direction": item.direction}
            for item in product_query.sorts
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
