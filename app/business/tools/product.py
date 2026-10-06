"""Product catalog, variant selection and product-data formatting."""

import json
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from app.business.intent import Intent, IntentDecision, MediaAction, is_product_catalog_query
from app.business.planner import ProductFilter, ProductQuery
from app.business.product_outcome import canonical_product_content, product_outcome
from app.business.product_query import (
    PRODUCT_FIELD_CATALOG,
    ProductFieldRequest,
    ProductReference,
    ambiguity_choice,
    fallback_field_requests,
    product_query_from_dict,
    product_query_to_dict,
    validate_product_query,
)
from app.business.product_selection import (
    choice_index,
    has_product_identifier,
    is_product_follow_up,
    mentions_identifier,
    mentions_identifier_alias,
    narrow_variants,
    product_fields,
    product_identifier_tokens,
)
from app.business.response_style import format_key_value_lines
from app.business.store import BusinessResult
from app.business.tools.common import RoutedReply

logger = structlog.get_logger()

_STRUCTURED_FIELD_LABELS = {
    "model": "产品型号",
    "series": "系列",
    "description": "产品描述",
    "material": "材质",
    "spray_modes": "档位",
    "color": "颜色",
    "special_function": "特殊功能",
    "shipping_origin": "出货地",
    "set_price": "套装价",
    "body_price": "主体价",
    "cost_price": "成本价",
    "piece_count": "件数",
    "supplier": "供应商",
    "body_supplier": "主体供应商",
    "components": "配件",
    "images": "图片",
    "videos": "视频",
}

_FILTER_FIELD_LABELS = {
    key: _STRUCTURED_FIELD_LABELS[key]
    for key in (
        "series",
        "material",
        "spray_modes",
        "color",
        "special_function",
        "shipping_origin",
    )
}

_MAX_PRODUCT_CHOICES = 3
_CATALOG_PAGE_SIZE = 5
_FAMILY_DETAIL_PAGE_SIZE = 1
_EMPTY_PRODUCT_FIELD_VALUES = frozenset({"", "无", "未登记", "暂无", "资料中未登记"})


class ProductToolMixin:
    """Product methods mixed into the public BusinessAgent."""

    @staticmethod
    def _product_field_map(product: BusinessResult) -> dict[str, str]:
        return product_fields(product.content)

    def _clear_product_context(self, user_id: str) -> None:
        for state in (
            self._last_product_by_user,
            self._last_media_by_user,
            self._product_choices,
            self._product_catalog_context,
            self._product_query_context,
            self._product_shadow_query_context,
        ):
            self._state_pop(state, user_id)

    @staticmethod
    def _catalog_page_direction(query: str) -> int | None:
        normalized = query.strip().lower()
        if any(marker in normalized for marker in ("上一页", "上页")):
            return -1
        if any(marker in normalized for marker in ("下一页", "下页", "继续看")):
            return 1
        return None

    def _catalog_page_requested(self, user_id: str, query: str) -> bool:
        return self._catalog_page_direction(query) is not None and isinstance(
            self._state_get(self._product_catalog_context, user_id), dict
        )

    def _ask_product_choice(
        self,
        user_id: str,
        products: list[BusinessResult],
        query: str,
        decision: IntentDecision,
        *,
        prefix: str = "",
    ) -> RoutedReply:
        self._state_pop(self._last_product_by_user, user_id)
        if len(products) > _MAX_PRODUCT_CHOICES:
            self._state_pop(self._product_choices, user_id)
            return RoutedReply(
                decision.intent,
                (
                    f"找到 {len(products)} 款相关产品。为避免一次展示过多内容，请补充具体型号、"
                    "系列、颜色或材质；例如“LY0669GT 配件价格”。"
                    "如果要查看产品目录，请说“查看产品目录”。"
                ),
                [],
            )
        visible = products
        self._product_choices[user_id] = {
            "ids": [item.source_id for item in visible],
            "query": query,
            "intent": str(decision.intent),
            "media_action": str(decision.media_action) if decision.media_action else None,
            "media_type": decision.media_type,
            "media_role": decision.media_role,
        }
        lines = [prefix or f"找到 {len(products)} 款相关产品，请先确认具体型号："]
        for index, product in enumerate(visible, 1):
            fields = self._product_field_map(product)
            details = [
                (label, fields[key][:100])
                for key, label in (
                    ("颜色", "颜色"),
                    ("材质", "材质"),
                    ("档位", "档位"),
                    ("套装价", "套装价"),
                    ("主体价", "主体价"),
                )
                if fields.get(key) and fields[key] not in {"无", "未登记"}
            ]
            lines.append("")
            lines.append(f"{index}. {product.title}")
            lines.extend(format_key_value_lines(details))
        lines.append("请回复序号（如“第二个”），或补充型号、颜色或材质。")
        return RoutedReply(decision.intent, "\n".join(lines), [])

    def _resume_product_choice(
        self, user_id: str, query: str, fallback: IntentDecision
    ) -> IntentDecision | None:
        pending = self._state_get(self._product_choices, user_id)
        if not isinstance(pending, dict):
            return None
        if fallback.intent in {Intent.POLICY, Intent.REPLY_SCRIPT} or is_product_catalog_query(
            query
        ):
            self._clear_product_context(user_id)
            return None
        index = choice_index(query)
        try:
            indexed = [self.store.get_active_product(key) for key in pending["ids"]]
        except Exception as exc:
            logger.warning("product_choices_refresh_failed", error_type=type(exc).__name__)
            return None
        products = [item for item in indexed if item is not None]
        if (
            has_product_identifier(query)
            and index is None
            and not any(
                mentions_identifier(query, self._product_field_map(item).get("产品型号", ""))
                for item in products
            )
        ):
            self._clear_product_context(user_id)
            return None
        narrowed = narrow_variants(query, products)
        if index is not None:
            selected = indexed[index] if 0 <= index < len(indexed) else None
            pending["selected_id"] = selected.source_id if selected else ""
            pending["selection_error"] = not bool(selected)
        elif len(narrowed) < len(products):
            pending["selected_id"] = narrowed[0].source_id if len(narrowed) == 1 else ""
            pending["selection_error"] = not narrowed
            if narrowed:
                pending["ids"] = [item.source_id for item in narrowed]
        elif not is_product_follow_up(query):
            self._state_pop(self._product_choices, user_id)
            return None
        self._product_choices[user_id] = pending
        saved_intent = Intent(pending["intent"])
        action_requested = fallback.intent in {Intent.PRODUCT, Intent.PRODUCT_MEDIA}
        intent = fallback.intent if action_requested else saved_intent
        return IntentDecision(
            intent=intent,
            confidence=0.99,
            reason="员工正在确认上一轮的具体产品型号",
            query_for_search=query if action_requested else str(pending["query"]),
            source="conversation_state",
            media_action=(
                fallback.media_action
                if action_requested
                else MediaAction(pending["media_action"])
                if pending.get("media_action")
                else None
            ),
            media_type=fallback.media_type if action_requested else pending.get("media_type"),
            media_role=fallback.media_role if action_requested else pending.get("media_role"),
        )

    def _resolve_product(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        embedding: list[float] | None = None,
        *,
        allow_family_overview: bool = False,
        requested_fields: tuple[str, ...] | None = None,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
    ) -> tuple[BusinessResult | None, RoutedReply | None]:
        pending = self._state_get(self._product_choices, user_id)
        if decision.source == "conversation_state" and isinstance(pending, dict):
            selected_id = pending.get("selected_id")
            if selected_id:
                product = self.store.get_active_product(str(selected_id))
                self._state_pop(self._product_choices, user_id)
                if product:
                    self._remember_product(user_id, product.source_id)
                    return product, None
                return None, RoutedReply(decision.intent, "该产品已停用，请重新查询产品。", [])
            products = [self.store.get_active_product(key) for key in pending["ids"]]
            products = [product for product in products if product is not None]
            if products:
                return None, self._ask_product_choice(
                    user_id,
                    products,
                    search_query,
                    decision,
                    prefix="当前选项中没有找到该产品，请重新选择："
                    if pending.get("selection_error")
                    else "请先确认具体型号：",
                )
            self._clear_product_context(user_id)
            return None, RoutedReply(decision.intent, "这些产品已停用，请重新查询产品。", [])
        previous_id = self._state_get(self._last_product_by_user, user_id)
        if previous_id and is_product_follow_up(original_query):
            product = self.store.get_active_product(str(previous_id))
            if product:
                self._remember_product(user_id, product.source_id)
                return product, None
            self._clear_product_context(user_id)
            return None, RoutedReply(decision.intent, "上次选择的产品已停用，请重新查询产品。", [])
        if (
            previous_id
            and not has_product_identifier(original_query)
            and any(
                word in original_query
                for word in ("那个", "那款", "这款", "它", "换成", "换个", "换一个")
            )
        ):
            product = self.store.get_active_product(str(previous_id))
            code = self._product_field_map(product).get("产品型号", "") if product else ""
            if code:
                search_query, embedding = f"{code} {original_query}", None
        if not has_product_identifier(original_query):
            self._state_pop(self._product_choices, user_id)
            return None, RoutedReply(
                decision.intent,
                (
                    "请先提供具体产品型号后再查询，例如“LY0669GT 配件价格”。"
                    "如果你想查看产品目录，请说“查看产品目录”。"
                ),
                [],
            )
        self._state_pop(self._last_product_by_user, user_id)
        self._state_pop(self._product_choices, user_id)
        actual_query = original_query if has_product_identifier(original_query) else search_query
        results = self.store.search_products(
            actual_query,
            limit=500,
            query_embedding=embedding if actual_query == search_query else None,
        )
        if not results and is_product_follow_up(original_query):
            results = self.store.list_active_products(limit=500)
        results = narrow_variants(actual_query, results)
        if not results:
            return None, RoutedReply(decision.intent, "产品库中没有找到对应产品。", [])
        if len(results) > 1:
            family_reference = self._product_family_reference(actual_query, results)
            if allow_family_overview and family_reference:
                if self._is_full_family_detail_query(original_query):
                    self._product_catalog_context[user_id] = {
                        "ids": [item.source_id for item in results],
                        "page": 1,
                        "mode": "family_detail",
                        "family_reference": family_reference,
                    }
                    return None, self._format_product_family_details(
                        results,
                        family_reference=family_reference,
                        page=1,
                        can_view_cost=can_view_cost,
                        cost_access_message=cost_access_message,
                    )
                if len(results) > _CATALOG_PAGE_SIZE:
                    self._product_catalog_context[user_id] = {
                        "ids": [item.source_id for item in results],
                        "page": 1,
                        "mode": "family_summary",
                        "family_reference": family_reference,
                        "query": original_query,
                        "requested_fields": list(requested_fields or ()),
                    }
                return None, self._format_product_family_summary(
                    results,
                    family_reference=family_reference,
                    query=original_query,
                    requested_fields=requested_fields,
                    page=1,
                    can_view_cost=can_view_cost,
                    cost_access_message=cost_access_message,
                )
            return None, self._ask_product_choice(user_id, results, actual_query, decision)
        self._remember_product(user_id, results[0].source_id)
        return results[0], None

    def _product_family_reference(
        self,
        query: str,
        products: list[BusinessResult],
    ) -> str | None:
        for token in product_identifier_tokens(query):
            matched = 0
            for product in products:
                model = self._product_field_map(product).get("产品型号", product.title)
                normalized_model = re.sub(r"\s+", "", model.casefold())
                if token in normalized_model:
                    matched += 1
            if matched > 1:
                return token
        return None

    @staticmethod
    def _is_full_family_detail_query(query: str) -> bool:
        return any(
            marker in query
            for marker in ("全部信息", "完整信息", "所有信息", "全部资料", "完整资料")
        )

    def _format_product_family_summary(
        self,
        products: list[BusinessResult],
        *,
        family_reference: str,
        query: str,
        requested_fields: tuple[str, ...] | None,
        page: int,
        can_view_cost: bool,
        cost_access_message: str,
    ) -> RoutedReply:
        family_label = family_reference.upper()
        requested = set(requested_fields or ())
        normalized = query.lower()
        cost_requested = "cost_price" in requested or any(
            marker in normalized for marker in ("成本价", "成本")
        )
        if cost_requested and not can_view_cost:
            return RoutedReply(Intent.PRODUCT, cost_access_message, [])
        price_requested = bool(requested.intersection({"set_price", "body_price", "cost_price"}))
        price_requested = price_requested or any(
            marker in normalized for marker in ("价格", "多少钱", "报价", "售价")
        )
        total = len(products)
        total_pages = max(1, (total + _CATALOG_PAGE_SIZE - 1) // _CATALOG_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * _CATALOG_PAGE_SIZE
        visible = products[start : start + _CATALOG_PAGE_SIZE]
        heading = f"{family_label}系列目前有 {total} 款"
        if total_pages > 1:
            heading += f"，第 {page}/{total_pages} 页"
        lines = [f"{heading}："]
        sources: list[dict[str, object]] = []
        for index, product in enumerate(visible, start + 1):
            fields = self._product_field_map(product)
            model = fields.get("产品型号", product.title)
            details: list[tuple[str, str]] = []
            if price_requested:
                for key, label in (
                    ("颜色", "颜色"),
                    ("材质", "材质"),
                    ("套装价", "套装价"),
                    ("主体价", "主体价"),
                ):
                    value = fields.get(key, "").strip()
                    if value and value not in _EMPTY_PRODUCT_FIELD_VALUES:
                        details.append((label, value))
                if cost_requested:
                    record = self.store.get_active_product_record(product.source_id)
                    cost_price = (record or {}).get("cost_price")
                    details.append(
                        (
                            "成本价",
                            f"{float(cost_price):.2f}元"
                            if cost_price is not None
                            else "资料中未登记",
                        )
                    )
            else:
                for key, label in (
                    ("系列", "系列"),
                    ("产品描述", "描述"),
                    ("材质", "材质"),
                    ("档位", "档位"),
                    ("颜色", "颜色"),
                    ("特殊功能", "特殊功能"),
                    ("出货地", "出货地"),
                    ("套装价", "套装价"),
                    ("主体价", "主体价"),
                    ("件数", "库存"),
                ):
                    value = fields.get(key, "").strip()
                    if value and value not in _EMPTY_PRODUCT_FIELD_VALUES:
                        details.append((label, value[:120]))
            lines.extend(("", f"{index}. {model}"))
            lines.extend(format_key_value_lines(details))
            sources.append(self.store.as_dict(product))
        lines.extend(
            (
                "",
                "如需查看某款的完整参数、配件和媒体数量，请回复完整型号。",
            )
        )
        if total_pages > 1:
            navigation = []
            if page > 1:
                navigation.append("上一页")
            if page < total_pages:
                navigation.append("下一页")
            lines.append(f"回复“{'”或“'.join(navigation)}”继续查看。")
        return RoutedReply(Intent.PRODUCT, "\n".join(lines), sources)

    def _format_product_family_details(
        self,
        products: list[BusinessResult],
        *,
        family_reference: str,
        page: int,
        can_view_cost: bool,
        cost_access_message: str,
    ) -> RoutedReply:
        family_label = family_reference.upper()
        total_pages = max(
            1, (len(products) + _FAMILY_DETAIL_PAGE_SIZE - 1) // _FAMILY_DETAIL_PAGE_SIZE
        )
        page = max(1, min(page, total_pages))
        start = (page - 1) * _FAMILY_DETAIL_PAGE_SIZE
        visible = products[start : start + _FAMILY_DETAIL_PAGE_SIZE]
        blocks = [f"{family_label}系列共 {len(products)} 款，完整资料第 {page}/{total_pages} 页："]
        sources: list[dict[str, object]] = []
        for product in visible:
            media = self.store.media_for_product(product.source_id)
            detail = self._format_product_detail(
                product,
                media,
                "",
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
            blocks.extend(("", detail.content))
            sources.extend(detail.sources)
        if total_pages > 1:
            navigation = []
            if page > 1:
                navigation.append("上一页")
            if page < total_pages:
                navigation.append("下一页")
            blocks.extend(("", f"回复“{'”或“'.join(navigation)}”继续查看。"))
        return RoutedReply(Intent.PRODUCT, "\n".join(blocks), sources)

    @staticmethod
    def _media_summary(media: list[BusinessResult]) -> tuple[str, str]:
        image_labels = {
            "product_image": "产品图",
            "body_image": "主体图",
            "configuration_image": "配置图",
        }
        image_groups: list[str] = []
        for role, label in image_labels.items():
            names = [
                item.title
                for item in media
                if item.media_type == "image" and item.media_role == role
            ]
            if names:
                image_groups.append(f"{label}{len(names)}张（{'、'.join(names)}）")
        other_images = [
            item.title
            for item in media
            if item.media_type == "image" and item.media_role not in image_labels
        ]
        if other_images:
            image_groups.append(f"其他图片{len(other_images)}张（{'、'.join(other_images)}）")
        image_count = sum(item.media_type == "image" for item in media)
        images = f"图片：共{image_count}张" + (
            f"，{'；'.join(image_groups)}" if image_groups else ""
        )
        video_names = [item.title for item in media if item.media_type == "video"]
        videos = f"视频：共{len(video_names)}个" + (
            f"（{'、'.join(video_names)}）" if video_names else ""
        )
        return images, videos

    @staticmethod
    def _component_quantity(name: str) -> int:
        match = re.search(r"(?:\*|×|x)\s*(\d+)\s*$", name.strip(), re.IGNORECASE)
        return max(1, int(match.group(1))) if match else 1

    def _component_summary(
        self,
        product: BusinessResult,
        *,
        can_view_cost: bool = False,
        record: dict[str, object] | None = None,
        include_prices: bool = False,
        include_total: bool = False,
    ) -> str:
        if record is None:
            getter = getattr(self.store, "get_active_product_record", None)
            if getter is None:
                getter = getattr(self.store, "get_product_record", None)
            if getter is not None:
                try:
                    record = getter(product.source_id)
                except Exception as exc:
                    logger.warning(
                        "product_components_lookup_failed", error_type=type(exc).__name__
                    )
                    record = None
        components = list((record or {}).get("components") or [])
        if not components:
            return "配件明细：资料中未登记"
        lines = [f"配件明细（共{len(components)}项）："]
        priced_total = Decimal("0")
        missing_prices = 0
        for index, item in enumerate(components, 1):
            details = [str(item.get("name") or "未命名配件")]
            quantity = self._component_quantity(details[0])
            supplier = str(item.get("supplier") or "").strip()
            if supplier:
                details.append(f"供应商：{supplier}")
            unit_price = item.get("unit_price")
            if can_view_cost and include_prices and unit_price is not None:
                details.append(f"采购价：{float(unit_price):g}元")
            if can_view_cost and include_total:
                if unit_price is None:
                    missing_prices += 1
                else:
                    try:
                        priced_total += Decimal(str(unit_price)) * quantity
                    except (InvalidOperation, TypeError, ValueError):
                        missing_prices += 1
            lines.append(f"{index}. " + "｜".join(details))
        if include_total and can_view_cost:
            if priced_total == 0 and missing_prices == len(components):
                lines.append("配件采购总价：资料中未登记")
            elif missing_prices:
                lines.append(
                    f"配件采购小计：{priced_total:.2f}元（还有{missing_prices}项未登记采购价）"
                )
            else:
                lines.append(f"配件采购总价：{priced_total:.2f}元")
        return "\n".join(lines)

    def _format_product_detail(
        self,
        product: BusinessResult,
        media: list[BusinessResult],
        query: str = "",
        *,
        requested_fields: tuple[str, ...] | None = None,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
    ) -> RoutedReply:
        fields = self._product_field_map(product)
        image_summary, video_summary = self._media_summary(media)
        getter = getattr(self.store, "get_active_product_record", None)
        record = None
        if getter is not None:
            try:
                record = getter(product.source_id)
            except Exception as exc:
                logger.warning(
                    "product_sensitive_fields_lookup_failed", error_type=type(exc).__name__
                )
        total_markers = ("总价", "小计", "合计", "采购总额", "总金额")
        component_price_markers = ("采购价", "进价", "价格", "多少钱", "单价")
        normalized = query.lower()
        if requested_fields is None:
            component_price_query = any(marker in normalized for marker in ("采购价", "进价")) or (
                "配件" in normalized
                and any(marker in normalized for marker in component_price_markers)
            )
            sensitive_price_query = component_price_query or any(
                marker in normalized for marker in ("成本价", "成本")
            )
        else:
            component_price_query = "components" in requested_fields and (
                "cost_price" in requested_fields
                or any(marker in normalized for marker in component_price_markers + total_markers)
            )
            sensitive_price_query = component_price_query or "cost_price" in requested_fields
        component_total_query = "配件" in normalized and any(
            marker in normalized for marker in total_markers
        )
        component_summary = self._component_summary(
            product,
            can_view_cost=can_view_cost,
            record=record,
            include_prices=component_price_query,
            include_total=component_total_query,
        )
        sensitive_content = ""
        if sensitive_price_query:
            if not can_view_cost:
                sensitive_content = cost_access_message
            elif component_price_query:
                sensitive_content = (
                    component_summary.splitlines()[-1]
                    if component_total_query
                    else component_summary
                )
            else:
                cost_price = (record or {}).get("cost_price")
                sensitive_content = (
                    f"成本价：{float(cost_price):.2f}元"
                    if cost_price is not None
                    else "成本价：资料中未登记"
                )
        field_queries = (
            ("body_price", "主体价", "主体多少钱"),
            ("set_price", "套装价", "价格", "多少钱", "报价", "售价"),
            ("piece_count", "件数", "库存", "多少件", "几件", "还剩多少", "还有多少"),
            ("material", "材质"),
            ("spray_modes", "档位", "几档"),
            ("color", "颜色"),
            ("images", "图片", "照片", "产品图", "主体图", "配置图"),
            ("videos", "视频"),
            ("components", "配件", "包含什么", "都有什么"),
            ("model", "产品型号"),
            ("series", "系列"),
            ("description", "产品描述"),
            ("special_function", "特殊功能"),
            ("shipping_origin", "出货地"),
            ("supplier", "供应商"),
            ("body_supplier", "主体供应商"),
        )
        matched: list[str] = []
        for field, label, *markers in field_queries:
            price_markers = {"价格", "多少钱", "报价", "售价"}
            matches = (
                [
                    marker
                    for marker in markers
                    if marker not in price_markers or "主体" not in normalized
                ]
                if field == "set_price"
                else markers
            )
            selected = (
                field in requested_fields
                if requested_fields is not None
                else any(marker in normalized for marker in (label, *matches))
            )
            if selected:
                if field == "components" and component_price_query and sensitive_content:
                    continue
                if field == "videos":
                    content = video_summary
                elif field == "images":
                    content = image_summary
                elif field == "components":
                    content = component_summary
                else:
                    content = f"{label}：{fields.get(label, '资料中未登记')}"
                matched.append(content)
        if sensitive_content:
            matched.insert(0, sensitive_content)
        if matched:
            sources = [
                self.store.as_dict(product),
                *[self.store.as_dict(item) for item in media],
            ]
            return RoutedReply(Intent.PRODUCT, "\n".join(matched), sources)
        ordered = (
            ("产品型号", "产品型号"),
            ("系列", "系列"),
            ("产品描述", "产品描述"),
            ("材质", "材质"),
            ("档位", "档位"),
            ("颜色", "颜色"),
            ("特殊功能", "特殊功能"),
            ("出货地", "出货地"),
            ("套装价", "套装价"),
            ("成本价", "成本价"),
            ("主体价", "主体价"),
            ("件数", "件数"),
            ("供应商", "供应商"),
            ("主体供应商", "主体供应商"),
            ("备注", "备注"),
            ("价格口径", "价格口径"),
        )
        lines: list[str] = []
        cost_price = (record or {}).get("cost_price")
        for key, label in ordered:
            if key == "成本价":
                if can_view_cost and cost_price is not None:
                    lines.append(f"成本价：{float(cost_price):.2f}元")
            elif fields.get(key):
                lines.append(f"{label}：{fields[key]}")
        lines.append(component_summary)
        lines.extend((image_summary, video_summary))
        sources = [self.store.as_dict(product), *[self.store.as_dict(item) for item in media]]
        return RoutedReply(
            Intent.PRODUCT, "\n".join(lines) or "产品资料中没有登记可展示的信息。", sources
        )

    def _format_product_catalog(
        self,
        products: list[BusinessResult],
        *,
        intro: str | None = None,
        page: int = 1,
        display_fields: tuple[str, ...] = (),
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
    ) -> RoutedReply:
        if not products:
            return RoutedReply(Intent.PRODUCT, "当前没有已发布产品。", [])
        total = len(products)
        total_pages = max(1, (total + _CATALOG_PAGE_SIZE - 1) // _CATALOG_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * _CATALOG_PAGE_SIZE
        visible = products[start : start + _CATALOG_PAGE_SIZE]
        if intro:
            heading_base = intro.rstrip("：")
            heading = (
                f"{heading_base}："
                if total_pages == 1
                else f"{heading_base}，第 {page}/{total_pages} 页"
                f"（每页最多 {_CATALOG_PAGE_SIZE} 款）："
            )
        elif total_pages == 1:
            heading = f"已发布产品共展示 {total} 款："
        else:
            heading = (
                f"已发布产品共 {total} 款，第 {page}/{total_pages} 页"
                f"（每页最多 {_CATALOG_PAGE_SIZE} 款）："
            )
        blocks: list[str] = [heading]
        sources: list[dict[str, object]] = []
        for index, product in enumerate(visible, start + 1):
            media = self.store.media_for_product(product.source_id)
            fields = self._product_field_map(product)
            model = fields.get("产品型号") or product.title or f"未命名产品 {index}"
            summary_fields = [
                ("系列", "系列"),
                ("颜色", "颜色"),
                ("材质", "材质"),
                ("档位", "档位"),
                ("套装价", "套装价"),
                ("主体价", "主体价"),
                ("件数", "库存"),
            ]
            if "special_function" in display_fields:
                summary_fields.insert(4, ("特殊功能", "特殊功能"))
            details = [
                (label, fields[key])
                for key, label in summary_fields
                if fields.get(key) and fields[key] not in _EMPTY_PRODUCT_FIELD_VALUES
            ]
            description = fields.get("产品描述", "").strip()
            blocks.append("")
            blocks.append(f"{index}. {model}")
            if description and description not in _EMPTY_PRODUCT_FIELD_VALUES:
                blocks.append(f"描述：{description[:100]}")
            blocks.extend(format_key_value_lines(details))
            sources.extend(self.store.as_dict(item) for item in [product, *media])
        example_model = self._product_field_map(products[0]).get("产品型号", products[0].title)
        blocks.extend(
            (
                "",
                f"回复“查看+型号”（例如“查看{example_model}”）可获取完整参数、配件和媒体资料。",
            )
        )
        if total_pages > 1:
            navigation = []
            if page > 1:
                navigation.append("上一页")
            if page < total_pages:
                navigation.append("下一页")
            blocks.append(f"回复“{'”或“'.join(navigation)}”继续查看。")
        return RoutedReply(Intent.PRODUCT, "\n".join(blocks), sources)

    def _catalog_page_reply(
        self,
        user_id: str,
        query: str,
        *,
        can_view_cost: bool,
        cost_access_message: str,
    ) -> RoutedReply | None:
        context = self._state_get(self._product_catalog_context, user_id)
        if not isinstance(context, dict):
            return None
        direction = self._catalog_page_direction(query)
        if direction is None:
            return None
        ids = context.get("ids")
        if not isinstance(ids, list):
            self._state_pop(self._product_catalog_context, user_id)
            return None
        products = [self.store.get_active_product(str(item)) for item in ids]
        products = [item for item in products if item is not None]
        if not products:
            self._state_pop(self._product_catalog_context, user_id)
            return RoutedReply(Intent.PRODUCT, "当前产品目录已没有可展示的产品。", [])
        current_page = int(context.get("page") or 1)
        mode = str(context.get("mode") or "catalog")
        page_size = _FAMILY_DETAIL_PAGE_SIZE if mode == "family_detail" else _CATALOG_PAGE_SIZE
        total_pages = max(1, (len(products) + page_size - 1) // page_size)
        next_page = current_page + direction
        if next_page < 1 or next_page > total_pages:
            return RoutedReply(
                Intent.PRODUCT,
                "已经是产品目录的第一页。" if direction < 0 else "已经是产品目录的最后一页。",
                [],
            )
        context["page"] = next_page
        self._product_catalog_context[user_id] = context
        if mode == "family_detail":
            return self._format_product_family_details(
                products,
                family_reference=str(context.get("family_reference") or "相关型号"),
                page=next_page,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
        if mode == "family_summary":
            requested_fields = context.get("requested_fields")
            if not isinstance(requested_fields, list):
                requested_fields = []
            return self._format_product_family_summary(
                products,
                family_reference=str(context.get("family_reference") or "相关型号"),
                query=str(context.get("query") or ""),
                requested_fields=tuple(str(item) for item in requested_fields),
                page=next_page,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
        display_fields = context.get("display_fields")
        if not isinstance(display_fields, list):
            display_fields = []
        intro = context.get("intro")
        return self._format_product_catalog(
            products,
            intro=str(intro) if intro else None,
            page=next_page,
            display_fields=tuple(str(item) for item in display_fields),
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
        )

    def _resolve_product_references(
        self,
        references: tuple[str, ...],
        products: list[BusinessResult],
        *,
        purpose: str,
        allow_family: bool = False,
    ) -> tuple[list[BusinessResult], RoutedReply | None]:
        resolved: list[BusinessResult] = []
        for reference in references:
            matches = [
                product
                for product in products
                if mentions_identifier_alias(
                    reference,
                    self._product_field_map(product).get("产品型号", product.title),
                )
            ]
            if not matches:
                return (
                    [],
                    RoutedReply(
                        Intent.PRODUCT,
                        f"没有找到要{purpose}的产品型号“{reference.upper()}”，请确认完整型号。",
                        [],
                    ),
                )
            if len(matches) > 1:
                if not allow_family or not self._product_family_reference(reference, matches):
                    choices = "、".join(
                        self._product_field_map(product).get("产品型号", product.title)
                        for product in matches
                    )
                    return (
                        [],
                        RoutedReply(
                            Intent.PRODUCT,
                            f"“{reference.upper()}”可对应多个产品：{choices}。请补充完整型号。",
                            [],
                        ),
                    )
            for match in matches:
                if match.source_id not in {item.source_id for item in resolved}:
                    resolved.append(match)
        return resolved, None

    def _filter_structured_products(
        self,
        products: list[BusinessResult],
        filters: tuple[ProductFilter, ...],
    ) -> list[BusinessResult]:
        selected = products
        for item in filters:
            label = _FILTER_FIELD_LABELS[item.field]
            expected = item.value.casefold().strip()
            filtered: list[BusinessResult] = []
            for product in selected:
                field_value = self._product_field_map(product).get(label, "").casefold().strip()
                if item.operator == "equals":
                    matches = field_value == expected
                elif item.operator == "is_present":
                    matches = field_value not in _EMPTY_PRODUCT_FIELD_VALUES
                else:
                    matches = expected in field_value
                if matches:
                    filtered.append(product)
            selected = filtered
        return selected

    @staticmethod
    def _catalog_filter_description(
        filters: tuple[ProductFilter, ...],
    ) -> str | None:
        for item in filters:
            if item.field != "special_function":
                continue
            if item.operator == "is_present":
                return "已登记特殊功能"
            if item.operator == "contains":
                return f"特殊功能包含“{item.value}”"
            if item.operator == "equals":
                return f"特殊功能为“{item.value}”"
        return None

    def _structured_catalog(
        self,
        user_id: str,
        product_query: ProductQuery,
        *,
        can_view_cost: bool,
        cost_access_message: str,
    ) -> RoutedReply:
        products = self.store.list_active_products(limit=500)
        self._clear_product_context(user_id)
        if product_query.include_models:
            products, error = self._resolve_product_references(
                product_query.include_models,
                products,
                purpose="查询",
                allow_family=True,
            )
            if error:
                return error
        products = self._filter_structured_products(products, product_query.filters)
        filter_description = self._catalog_filter_description(product_query.filters)
        if not products:
            if filter_description:
                return RoutedReply(
                    Intent.PRODUCT,
                    f"当前没有{filter_description}的已发布产品。",
                    [],
                )
            return RoutedReply(Intent.PRODUCT, "当前没有符合条件的已发布产品。", [])
        excluded_models: list[str] = []
        if product_query.exclude_models:
            excluded, error = self._resolve_product_references(
                product_query.exclude_models,
                self.store.list_active_products(limit=500),
                purpose="排除",
            )
            if error:
                return error
            excluded_ids = {item.source_id for item in excluded}
            excluded_models = [
                self._product_field_map(item).get("产品型号", item.title) for item in excluded
            ]
            before_exclusion = products
            products = [item for item in products if item.source_id not in excluded_ids]
            if not products:
                joined = "、".join(excluded_models)
                content = (
                    f"目前已发布产品只有 {joined}，除此之外暂无其他产品。"
                    if len(before_exclusion) == 1
                    else f"除 {joined} 外，暂无其他符合条件的已发布产品。"
                )
                return RoutedReply(
                    Intent.PRODUCT,
                    content,
                    [self.store.as_dict(product) for product in before_exclusion],
                )
        if len(products) == 1 and not excluded_models:
            self._remember_product(user_id, products[0].source_id)
        intro = (
            f"除 {'、'.join(excluded_models)} 外，已发布产品还有 {len(products)} 款："
            if excluded_models
            else (
                f"找到 {len(products)} 款{filter_description}的产品："
                if filter_description
                else (
                    f"{product_query.include_models[0].upper()}系列目前有 {len(products)} 款："
                    if product_query.include_models
                    and self._product_family_reference(product_query.include_models[0], products)
                    else None
                )
            )
        )
        display_fields = tuple(
            dict.fromkeys(
                (
                    *product_query.fields,
                    *(item.field for item in product_query.filters),
                )
            )
        )
        if len(products) > _CATALOG_PAGE_SIZE:
            self._product_catalog_context[user_id] = {
                "ids": [item.source_id for item in products],
                "page": 1,
                "display_fields": list(display_fields),
                "intro": intro,
            }
        return self._format_product_catalog(
            products,
            intro=intro,
            display_fields=display_fields,
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
        )

    def _answer_product_legacy(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        product_query: ProductQuery | None = None,
        prefetched_embedding: list[float] | None = None,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
        **_: object,
    ) -> RoutedReply:
        catalog_page = self._catalog_page_reply(
            user_id,
            original_query,
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
        )
        if catalog_page is not None:
            return catalog_page
        if product_query is not None and product_query.operation in {"list", "compare"}:
            return self._structured_catalog(
                user_id,
                product_query,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
        if product_query is None and (
            is_product_catalog_query(original_query) or is_product_catalog_query(search_query)
        ):
            # Compatibility for direct internal callers; graph execution always supplies
            # a structured product query.
            product_query = ProductQuery(operation="list")
            return self._structured_catalog(
                user_id,
                product_query,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
        if product_query is not None and product_query.include_models:
            search_query = " ".join((*product_query.include_models, search_query))
            prefetched_embedding = None
        settings = self._business_settings()
        embedding = prefetched_embedding
        if (
            embedding is None
            and getattr(settings, "embedding_api_key", None)
            and getattr(settings, "embedding_model", None)
        ):
            try:
                embedding = self._business_rag_agent().embed(
                    [" ".join(search_query.split())[:500]]
                )[0]
            except Exception as exc:
                logger.warning("product_embedding_failed", error_type=type(exc).__name__)
        try:
            product, clarification = self._resolve_product(
                user_id,
                original_query,
                search_query,
                decision,
                embedding,
                allow_family_overview=True,
                requested_fields=(
                    product_query.fields
                    if product_query is not None and product_query.fields
                    else None
                ),
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            )
        except Exception as exc:
            logger.error("product_search_failed", error_type=type(exc).__name__)
            return RoutedReply(decision.intent, "产品资料查询暂时不可用，请稍后重试。", [])
        if clarification:
            return clarification
        try:
            media = self.store.media_for_product(product.source_id)
        except Exception as exc:
            logger.error("product_media_lookup_failed", error_type=type(exc).__name__)
            media = []
        display_query = search_query if decision.source == "conversation_state" else original_query
        return self._format_product_detail(
            product,
            media,
            display_query,
            requested_fields=(
                product_query.fields if product_query is not None and product_query.fields else None
            ),
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
        )

    @staticmethod
    def _v2_source(item: dict[str, object]) -> dict[str, object]:
        return {
            "source_type": "product",
            "source_id": str(item["id"]),
            "title": str(item.get("model") or ""),
            "content": "",
            "score": 1.0,
        }

    @staticmethod
    def _v2_value(field: str, value: object) -> str:
        if value is None or value == "" or value == []:
            return "资料未登记"
        if field in {"set_price", "body_price", "cost_price"}:
            return f"{float(value):.2f}元"
        if field == "piece_count":
            return f"{value}件"
        return str(value)

    def _v2_components(self, item: dict[str, object], *, include_prices: bool) -> str:
        components = item.get("components")
        if not isinstance(components, list) or not components:
            return "配件：资料未登记"
        lines = [f"配件明细（共{len(components)}项）："]
        for index, component in enumerate(components, 1):
            if not isinstance(component, dict):
                continue
            parts = [str(component.get("name") or "未命名配件")]
            if component.get("supplier"):
                parts.append(f"供应商：{component['supplier']}")
            if include_prices and component.get("unit_price") is not None:
                parts.append(f"采购价：{float(component['unit_price']):g}元")
            lines.append(f"{index}. " + "｜".join(parts))
        return "\n".join(lines)

    def _format_product_v2(
        self,
        result,
        product_query: ProductQuery,
        *,
        can_view_cost: bool,
    ) -> RoutedReply:
        if not result.items:
            return RoutedReply(Intent.PRODUCT, "当前没有符合条件的已发布产品。", [])
        requested = tuple(
            dict.fromkeys(item.field for item in product_query.effective_field_requests())
        )
        if not requested and (
            product_query.original_fields
            or (product_query.operation == "detail" and not product_query.allow_overview)
        ):
            return RoutedReply(
                Intent.PRODUCT, "无法可靠确认你要查询的字段，请说明想了解哪项产品信息。", []
            )
        full_detail = product_query.operation == "detail" and not requested
        fields = requested or (
            ("model", "series", "material", "color", "special_function", "set_price")
            if product_query.operation == "recommend"
            else (
                "model",
                "series",
                "description",
                "material",
                "spray_modes",
                "color",
                "special_function",
                "shipping_origin",
                "set_price",
                "body_price",
                "piece_count",
                "supplier",
                "body_supplier",
                "components",
            )
        )
        if "model" not in fields and len(result.items) > 1:
            fields = ("model", *fields)

        total_pages = max(1, (result.total + result.page_size - 1) // result.page_size)
        blocks: list[str] = []
        if result.total > 1:
            blocks.append(
                f"找到 {result.total} 款符合条件的产品，第 {result.page}/{total_pages} 页："
            )
        for index, item in enumerate(result.items, (result.page - 1) * result.page_size + 1):
            lines: list[str] = []
            if result.total > 1:
                lines.append(f"{index}. {item.get('model') or '资料未登记'}")
            column_by_field = {
                "description": "product_description",
                **{name: name for name in PRODUCT_FIELD_CATALOG},
            }
            for field in fields:
                if field in {"images", "videos"}:
                    continue
                if field == "model" and result.total > 1:
                    continue
                if field == "components":
                    lines.append(
                        self._v2_components(
                            item,
                            include_prices=can_view_cost and "cost_price" in requested,
                        )
                    )
                    continue
                label = PRODUCT_FIELD_CATALOG[field].label
                value = item.get(column_by_field[field])
                lines.append(f"{label}：{self._v2_value(field, value)}")
            if product_query.operation == "recommend":
                reason_fields = tuple(
                    dict.fromkeys(item_filter.field for item_filter in product_query.filters)
                ) or ("special_function", "material", "color", "set_price")
                facts = []
                for field in reason_fields:
                    if field not in column_by_field or field in {"components", "cost_price"}:
                        continue
                    value = item.get(column_by_field[field])
                    if value not in (None, "", []):
                        facts.append(
                            f"{PRODUCT_FIELD_CATALOG[field].label}为{self._v2_value(field, value)}"
                        )
                if facts:
                    lines.append("推荐依据：" + "、".join(facts))
            blocks.append("\n".join(lines))
        if total_pages > 1:
            actions = []
            if result.page > 1:
                actions.append("上一页")
            if result.page < total_pages:
                actions.append("下一页")
            if actions:
                blocks.append(f"回复“{'”或“'.join(actions)}”继续查看。")
        if full_detail and result.total > 1:
            blocks.append("完整资料已按每页1款展示。")
        return RoutedReply(
            Intent.PRODUCT,
            "\n\n".join(block for block in blocks if block),
            [self._v2_source(item) for item in result.items],
        )

    def _prepare_product_query_v2(
        self,
        user_id: str,
        original_query: str,
        product_query: ProductQuery,
        *,
        saved: object = None,
    ) -> ProductQuery:
        saved_query = (
            product_query_from_dict(saved.get("plan")) if isinstance(saved, dict) else None
        )
        pending = saved.get("pending_ambiguity") if isinstance(saved, dict) else None
        choice = ambiguity_choice(original_query)
        if (
            isinstance(pending, dict)
            and pending.get("code") == "set_material_or_components"
            and choice
            and not has_product_identifier(original_query)
        ):
            pending_query = product_query_from_dict(pending.get("plan"))
            if pending_query is not None:
                field = choice
                references = tuple(
                    ProductReference(item.value, item.kind, item.value, "context")
                    for item in pending_query.effective_references()
                )
                product_query = replace(
                    pending_query,
                    references=references,
                    include_models=tuple(item.value for item in references),
                    requested_fields=(ProductFieldRequest(field, original_query, "context"),),
                    fields=(field,),
                    original_fields=(field,),
                    ambiguities=(),
                    context_mode="continuation",
                )

        direction = self._catalog_page_direction(original_query)
        if (
            direction is not None
            and saved_query is not None
            and not has_product_identifier(original_query)
        ):
            return replace(
                saved_query,
                references=tuple(
                    replace(item, source="context") for item in saved_query.effective_references()
                ),
                requested_fields=tuple(
                    replace(item, source="context")
                    for item in saved_query.effective_field_requests()
                ),
                filters=tuple(replace(item, source="context") for item in saved_query.filters),
                page=max(1, saved_query.page + direction),
                context_mode="continuation",
            )
        # Keep rejected proposals visible to validation instead of silently erasing
        # them here; a forged context must never become an empty full-detail plan.
        if product_query.context_mode == "continuation" and saved_query is not None:
            if not product_query.effective_references():
                references = tuple(
                    ProductReference(item.value, item.kind, item.value, "context")
                    for item in saved_query.effective_references()
                )
                product_query = replace(
                    product_query,
                    references=references,
                    include_models=tuple(item.value for item in references),
                )
        return product_query

    def _rank_product_recommendations(
        self,
        items: list[dict[str, object]],
        original_query: str,
    ) -> list[dict[str, object]] | None:
        settings = self._business_settings()
        if not (getattr(settings, "llm_api_key", None) and getattr(settings, "llm_model", None)):
            return None
        facts = [
            {
                "id": str(item["id"]),
                "model": item.get("model"),
                "series": item.get("series"),
                "material": item.get("material"),
                "color": item.get("color"),
                "special_function": item.get("special_function"),
                "set_price": float(item["set_price"])
                if item.get("set_price") is not None
                else None,
                "body_price": float(item["body_price"])
                if item.get("body_price") is not None
                else None,
            }
            for item in items[:10]
        ]
        try:
            response = (
                self._business_rag_agent()
                ._llm_client()
                .chat.completions.create(
                    model=settings.llm_model,
                    temperature=0,
                    max_tokens=300,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你只对给定产品候选排序。不得添加、改写或推断事实。"
                                '只返回JSON：{"ordered_ids":["候选id"]}。'
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"request": original_query[:500], "candidates": facts},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                )
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            ordered = payload.get("ordered_ids", [])
            if not isinstance(ordered, list):
                return None
            by_id = {str(item["id"]): item for item in items}
            ids = [str(item) for item in ordered if str(item) in by_id]
            ids.extend(item_id for item_id in by_id if item_id not in ids)
            return [by_id[item_id] for item_id in ids]
        except Exception as exc:
            logger.warning("product_recommendation_ranking_failed", error_type=type(exc).__name__)
            return None

    def _answer_product_v2(
        self,
        user_id: str,
        original_query: str,
        product_query: ProductQuery,
        *,
        can_view_cost: bool,
        cost_access_message: str,
        shadow: bool = False,
    ) -> RoutedReply:
        state = self._product_shadow_query_context if shadow else self._product_query_context
        saved = self._state_get(state, user_id)
        trusted = product_query_from_dict(saved.get("plan")) if isinstance(saved, dict) else None
        product_query = replace(
            product_query,
            original_fields=tuple(
                dict.fromkeys(
                    (
                        *product_query.original_fields,
                        *(item.field for item in product_query.effective_field_requests()),
                    )
                )
            ),
        )
        proposed_fields = product_query.original_fields
        proposed_requests = [
            {"field": item.field, "evidence": item.evidence, "source": item.source}
            for item in product_query.effective_field_requests()
        ]
        pending = saved.get("pending_ambiguity") if isinstance(saved, dict) else None
        choice = ambiguity_choice(original_query)
        if (
            isinstance(pending, dict)
            and pending.get("code") == "set_material_or_components"
            and choice
            and not has_product_identifier(original_query)
            and trusted
        ):
            trusted = replace(
                trusted, requested_fields=(ProductFieldRequest(choice),), fields=(choice,)
            )
        if has_product_identifier(original_query):
            product_query = replace(product_query, context_mode="independent")
            saved = None
            trusted = None
            self._state_pop(state, user_id)
        product_query = self._prepare_product_query_v2(
            user_id, original_query, product_query, saved=saved
        )
        validation = validate_product_query(
            product_query,
            original_query,
            can_view_cost=can_view_cost,
            trusted_context=trusted,
        )
        trace: dict[str, object] = {
            "planner_version": "product-query-v2",
            "query_plan": product_query_to_dict(validation.query),
            "validation": {
                "errors": list(validation.errors),
                "removed_fields": list(validation.removed_fields),
                "permission_denied_fields": list(validation.permission_denied_fields),
                "ambiguities": [item.code for item in validation.ambiguities],
                "proposed_fields": list(proposed_fields),
                "proposed_requests": proposed_requests,
                "original_fields": list(validation.query.original_fields),
                "accepted_fields": list(validation.query.fields),
                "field_semantics_version": 2,
            },
        }

        def finish(reply: RoutedReply, result_type: str, reason: str = "") -> RoutedReply:
            trace["outcome"] = product_outcome(
                reply,
                requested_fields=validation.query.fields,
                result_type=result_type,
                reason=reason,
            )
            return replace(reply, execution_trace=trace)

        if validation.ambiguities:
            ambiguity = validation.ambiguities[0]
            if not shadow:
                state[user_id] = {
                    "plan": product_query_to_dict(validation.query),
                    "pending_ambiguity": {
                        "code": ambiguity.code,
                        "plan": product_query_to_dict(replace(validation.query, ambiguities=())),
                    },
                }
            choices = "、".join(ambiguity.options)
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    f"{ambiguity.question}请回复：{choices}。",
                    [],
                    execution_trace=trace,
                ),
                "clarification",
                ambiguity.code,
            )
        denied_filter_or_sort = any(
            item.field in validation.permission_denied_fields
            for item in (*product_query.filters, *product_query.sorts)
        )
        if validation.permission_denied_fields and (
            denied_filter_or_sort or not validation.query.effective_field_requests()
        ):
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    cost_access_message,
                    [],
                    execution_trace=trace,
                ),
                "permission_denied",
                "field_permission_denied",
            )
        if validation.errors:
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    "这个查询条件无法安全执行，请补充明确的产品型号或字段。",
                    [],
                    execution_trace=trace,
                ),
                "validation_failed",
                "invalid_plan",
            )
        query = validation.query
        if not query.effective_field_requests() and (
            query.original_fields or (query.operation == "detail" and not query.allow_overview)
        ):
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    "无法可靠确认你要查询的字段，请说明想了解哪项产品信息，例如主体材质或套装配件。",
                    [],
                ),
                "clarification",
                "all_requested_fields_removed" if query.original_fields else "unspecified_fields",
            )
        if query.operation == "detail" and not query.effective_references():
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    "请先提供具体产品型号，例如“LY0653GDT-T 材质”。",
                    [],
                    execution_trace=trace,
                ),
                "clarification",
                "missing_product_reference",
            )
        if query.operation == "detail" and not query.effective_field_requests():
            query = replace(query, page_size=1)
        trace["query_plan"] = product_query_to_dict(query)
        result = self.store.query_products_v2(query, include_sensitive=can_view_cost)
        if query.operation == "recommend" and result.items:
            ranked = self._rank_product_recommendations(result.items, original_query)
            if ranked is None:
                return finish(
                    RoutedReply(
                        Intent.PRODUCT,
                        "产品推荐暂时无法完成，请稍后重试。",
                        [],
                        execution_trace=trace,
                    ),
                    "error",
                    "recommendation_failed",
                )
            result = replace(result, items=ranked)
        reply = self._format_product_v2(result, query, can_view_cost=can_view_cost)
        if validation.permission_denied_fields:
            reply = replace(reply, content=f"{reply.content}\n{cost_access_message}")
        if validation.removed_fields:
            reply = replace(reply, content=f"{reply.content}\n另有字段未能可靠确认，请补充说明。")
        selected_ids = [str(item["id"]) for item in result.items]
        if not shadow:
            state[user_id] = {
                "plan": product_query_to_dict(query),
                "selected_product_ids": selected_ids,
                "page": result.page,
            }
        if len(selected_ids) == 1 and not shadow:
            self._remember_product(user_id, selected_ids[0])
        trace["execution"] = {
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "product_ids": selected_ids,
        }
        return finish(reply, "fact" if result.items else "no_result")

    @staticmethod
    def _shadow_diff(legacy: RoutedReply, v2: RoutedReply) -> dict[str, object]:
        legacy_ids = sorted(
            str(item.get("source_id") or item.get("id") or "")
            for item in legacy.sources
            if item.get("source_type") == "product"
        )
        v2_ids = sorted(
            str(item.get("source_id") or item.get("id") or "")
            for item in v2.sources
            if item.get("source_type") == "product"
        )
        old = legacy.execution_trace.get("outcome") or product_outcome(legacy)
        new = v2.execution_trace.get("outcome") or product_outcome(v2)
        comparisons = {
            "same_known_result": old["result_type"] != "unknown"
            and new["result_type"] != "unknown",
            "same_result_type": old["result_type"] == new["result_type"],
            "same_requested_fields": old["requested_fields"] == new["requested_fields"],
            "same_answered_fields": old["answered_fields"] == new["answered_fields"],
            # Compare sensitive values transiently; persist only the boolean and redacted summaries.
            "same_content": canonical_product_content(legacy.content)
            == canonical_product_content(v2.content),
            "same_permissions": old["permission_denied"] == new["permission_denied"],
        }
        return {
            "schema_version": 2,
            "same_product_ids": legacy_ids == v2_ids,
            "legacy_product_ids": legacy_ids,
            "v2_product_ids": v2_ids,
            "legacy_source_count": len(legacy.sources),
            "v2_source_count": len(v2.sources),
            **comparisons,
            "same_semantics": legacy_ids == v2_ids and all(comparisons.values()),
            "delivered_engine": "legacy",
            "legacy_outcome": old,
            "v2_outcome": new,
            "v2_validation": v2.execution_trace.get("validation", {}),
            "v2_execution": v2.execution_trace.get("execution", {}),
            "differences": [
                key.removeprefix("same_") for key, same in comparisons.items() if not same
            ]
            + ([] if legacy_ids == v2_ids else ["product_ids"]),
        }

    def _answer_product(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        product_query: ProductQuery | None = None,
        **kwargs: object,
    ) -> RoutedReply:
        settings = self._business_settings()
        mode = getattr(settings, "product_query_v2_mode", "shadow")
        can_view_cost = bool(kwargs.get("can_view_cost", False))
        cost_access_message = str(
            kwargs.get(
                "cost_access_message",
                "你当前没有查看成本价和采购价的权限。",
            )
        )
        if mode == "on" and product_query is not None:
            unsafe_fallback = decision.source not in {
                "planner_model",
                "product_query_context",
                "conversation_state",
            } and (
                bool(product_query.filters)
                or product_query.operation in {"search", "compare", "recommend"}
            )
            if unsafe_fallback:
                return RoutedReply(
                    Intent.PRODUCT,
                    "复杂产品筛选暂时无法安全解析，请稍后重试。",
                    [],
                )
            try:
                return self._answer_product_v2(
                    user_id,
                    original_query,
                    product_query,
                    can_view_cost=can_view_cost,
                    cost_access_message=cost_access_message,
                )
            except Exception as exc:
                logger.error("product_query_v2_failed", error_type=type(exc).__name__)
                return RoutedReply(
                    Intent.PRODUCT,
                    "产品资料查询暂时不可用，请稍后重试。",
                    [],
                )

        legacy = self._answer_product_legacy(
            user_id,
            original_query,
            search_query,
            decision,
            product_query=product_query,
            **kwargs,
        )
        if mode != "shadow" or product_query is None:
            return legacy
        try:
            legacy_fields = product_query.fields or tuple(
                item.field for item in fallback_field_requests(original_query)
            )
            legacy = replace(
                legacy,
                execution_trace={
                    **legacy.execution_trace,
                    "outcome": product_outcome(legacy, requested_fields=legacy_fields),
                },
            )
            shadow = self._answer_product_v2(
                user_id,
                original_query,
                product_query,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
                shadow=True,
            )
            trace = dict(shadow.execution_trace)
            trace["shadow_diff"] = self._shadow_diff(legacy, shadow)
            # Only inherit a shadow answer that the employee actually saw.
            if trace["shadow_diff"]["same_semantics"] and trace.get("execution"):
                self._product_shadow_query_context[user_id] = {
                    "plan": trace.get("query_plan", {}),
                }
            else:
                self._state_pop(self._product_shadow_query_context, user_id)
            trace["delivered_engine"] = "legacy"
            return replace(legacy, execution_trace=trace)
        except Exception as exc:
            logger.warning("product_query_v2_shadow_failed", error_type=type(exc).__name__)
            self._state_pop(self._product_shadow_query_context, user_id)
            return replace(
                legacy,
                execution_trace={
                    "planner_version": "product-query-v2",
                    "shadow_diff": {"error_type": type(exc).__name__},
                },
            )

    def index_product_record(self, product_id: str) -> int:
        search_text = self.store.build_product_search_text(product_id)
        if search_text is None:
            raise LookupError("产品不存在")
        settings = self._business_settings()
        if not getattr(settings, "embedding_api_key", None) or not getattr(
            settings, "embedding_model", None
        ):
            self.store.set_product_embedding_status(product_id, search_text, "not_requested")
            return 0
        self.store.set_product_embedding_status(product_id, search_text, "running")
        try:
            embedding = self._business_rag_agent().embed([search_text])[0]
            self.store.save_product_embedding(
                product_id, search_text, embedding, settings.embedding_model
            )
        except Exception as exc:
            self.store.set_product_embedding_status(
                product_id, search_text, "failed", str(exc)[:1000]
            )
            raise
        return 1


class ProductTool(ProductToolMixin):
    """Internal product boundary; transport and routing remain elsewhere."""

    name = "product"

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner._answer_product(*args, **kwargs)

    @staticmethod
    def _business_settings():
        from app.business import service

        return service.get_settings()

    @staticmethod
    def _business_rag_agent():
        from app.business import service

        return service.get_rag_agent()
