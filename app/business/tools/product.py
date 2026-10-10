"""Product catalog, variant selection and product-data formatting."""

import json
import re
import time
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from app.business import pending
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
from app.ontology.schema import DERIVED_DIMENSIONS, dimension_fields, is_aggregatable
from app.ontology.values import fields_sharing_value
from app.ontology.values import matching_values as ontology_matching_values
from app.ontology.values import value_key as ontology_value_key

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
# 统计类提问要把匹配项都取回来算，行数上限防止把整个库拉进内存。
# 超过上限时的口径：只统计前 N 款——所以这里取足够大，正常库规模不会碰到。
_AGGREGATE_MAX_ROWS = 500
# 统计结果（多少种颜色…）每页列几种。与产品清单一样支持「下一页」：
# 列不完时不截断、也不藏起来，而是给出页码提示，员工想看全就能看全。
_AGGREGATE_PAGE_SIZE = 10
# 取值目录的缓存时长。取值变化是"上架新产品"级别的低频，不值得每个提问都查一次库。
_PRODUCT_VALUE_CACHE_SECONDS = 600.0


def _value_key(value: str) -> str:
    """保留这个名字只为兼容模块内既有调用；实现在本体层的取值层。"""
    return ontology_value_key(value)


def _matching_values(values: tuple[str, ...], text: str) -> list[str]:
    return ontology_matching_values(values, text)
_FAMILY_DETAIL_PAGE_SIZE = 1
_EMPTY_PRODUCT_FIELD_VALUES = frozenset({"", "无", "未登记", "暂无", "资料中未登记"})


class ProductToolMixin:
    """Product methods mixed into the public BusinessAgent."""

    def _product_value_catalog(self) -> dict[str, tuple[str, ...]]:
        """取值目录（带 TTL 缓存）。

        目录要查库，但每个产品提问都查一次不值得：取值变化是"上架新产品"级别的低频，
        缓存 10 分钟足够。查库失败就当作没有目录（照旧不解析），不能因此让回答失败。
        """
        now = time.monotonic()
        cached = getattr(self, "_product_values_cache", None)
        cached_at = getattr(self, "_product_values_cache_at", 0.0)
        if cached is not None and now - cached_at < _PRODUCT_VALUE_CACHE_SECONDS:
            return cached
        try:
            catalog = self.store.product_text_field_values()
        except Exception as exc:  # noqa: BLE001
            logger.warning("product_value_catalog_failed", error_type=type(exc).__name__)
            catalog = {}
        self._product_values_cache = catalog
        self._product_values_cache_at = now
        return catalog

    @staticmethod
    def _value_key(value: str) -> str:
        """保留这个入口只为兼容模块内的旧调用；实现在模块级 `_value_key`。"""
        return _value_key(value)

    def _resolve_product_filter_values(self, query: ProductQuery) -> ProductQuery:
        """把筛选值钉到正确的字段上，并把写法归一到库里的标准写法。

        员工给的是"值"，不是"字段"。模型没有取值概念，只能猜：实测同一个颜色
        「镜面玻璃·镜面」被猜成 description 和 series（都 0 款），而「灰玻璃·枪灰」
        猜成 color 猜对了——同一个问题时对时错，全靠运气。

        规则刻意保守，**只在能唯一确定时才动手**：
          1. 模型选的字段里有匹配 → 不动；库里只有唯一写法且与输入不同 → 归一成库里的写法；
          2. 模型选的字段一个都匹配不上、而恰有**另一个**字段匹配 → 改到那个字段；
          3. 以上都不满足（多个字段都匹配、或谁都不匹配）→ 原样不动，不瞎猜。
        """
        catalog = self._product_value_catalog()
        if not catalog or not query.filters:
            return query

        resolved: list[ProductFilter] = []
        changed = False
        for item in query.filters:
            replacement = self._resolve_one_filter_value(item, catalog)
            changed = changed or replacement != item
            resolved.append(replacement)
        if not changed:
            return query
        return replace(query, filters=tuple(resolved))

    def _resolve_one_filter_value(
        self, item: ProductFilter, catalog: dict[str, tuple[str, ...]]
    ) -> ProductFilter:
        definition = PRODUCT_FIELD_CATALOG.get(item.field)
        # 只处理文本等值/包含筛选：数值区间是模型算出来的，改写会破坏语义。
        if definition is None or definition.value_type != "text":
            return item
        if item.operator not in {"contains", "equals"}:
            return item
        raw = item.value
        if isinstance(raw, (list, tuple)) or raw is None:
            return item
        text = str(raw).strip()
        if not text:
            return item

        own = _matching_values(catalog.get(item.field, ()), text)
        if own:
            # 库里只有唯一写法、且与员工写的不同 → 归一到库里的写法，让 contains 命中
            if len(own) == 1 and own[0] != text:
                return replace(item, value=own[0])
            return item

        matches = fields_sharing_value(catalog, text, exclude=item.field)
        if len(matches) != 1:
            # 多个字段都能匹配（例如"枪灰"同时命中枪灰/灰玻璃·枪灰），
            # 或者谁都不匹配——都不该由代码替员工决定，保持原样。
            return item
        field, found = next(iter(matches.items()))
        return replace(
            item,
            field=field,
            value=found[0] if len(found) == 1 else text,
            operator="equals" if len(found) == 1 else "contains",
        )

    @staticmethod
    def _identifier_field_message(
        label: str,
        field: str,
        items: list[dict[str, object]],
        ordered: list[tuple[str, int]],
    ) -> str:
        """标识符字段被要求"有多少种"时，如实说明并给出**真正能答的**数。

        以前这里只回一句"按它分类得不到有意义的结果"，那只是不撒谎，没解决问题。
        员工问"有几个系列"想要的是**型号族群**（业务上的系列）——
        本体里声明了派生维度 family，所以这里能顺手把它算出来给出真答案。
        """
        head = (
            f"{label}在库里是逐款登记的（{len(items)} 款就有 {len(ordered)} 个不同的名字），"
            f"不能按它分类。"
        )
        derived = DERIVED_DIMENSIONS.get("family")
        # "能统计什么"由本体生成，不再手写"颜色、材质、档位"——
        # 以后新增维度字段自动出现在提示里，不会和 schema 脱节。
        hint = "想统计分类的话，" + "、".join(
            PRODUCT_FIELD_CATALOG[name].label for name in dimension_fields()[:5]
        ) + "这些可以。"
        if derived is None or field not in {"series", "model"}:
            return head + hint

        families: dict[str, int] = {}
        for item in items:
            value = derived.compute(item)
            if value:
                families[value] = families.get(value, 0) + 1
        if not families:
            return head + hint
        top = sorted(families.items(), key=lambda pair: (-pair[1], pair[0]))
        shown = "、".join(f"{name}（{count} 款）" for name, count in top[:10])
        more = f"，另有 {len(top) - 10} 个" if len(top) > 10 else ""
        return (
            f"{head}按{derived.label}的另一个口径看——{derived.explain}——"
            f"一共有 {len(families)} 个{derived.label}：{shown}{more}。"
        )

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

    @staticmethod
    def _catalog_page_target(query: str) -> int | None:
        """「第N页」是绝对页码，与「下一页」这类相对翻页不同。

        只在**已经存在产品目录上下文**时才当作翻页（见 `_catalog_page_requested`）：
        没有产品清单时，「第2页」仍然是问制度 PDF 的页码，这一点与改动前一致。
        """
        match = re.search(r"第\s*(\d+)\s*页", query)
        if not match:
            return None
        try:
            target = int(match.group(1))
        except ValueError:  # pragma: no cover - 正则已限定为数字
            return None
        return target if target >= 1 else None

    def _catalog_page_requested(self, user_id: str, query: str) -> bool:
        if not isinstance(self._state_get(self._product_catalog_context, user_id), dict):
            return False
        return (
            self._catalog_page_direction(query) is not None
            or self._catalog_page_target(query) is not None
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
        pending_request = {
            "ids": [item.source_id for item in products],
            "query": query,
            "intent": str(decision.intent),
            "media_action": str(decision.media_action) if decision.media_action else None,
            "media_type": decision.media_type,
            "media_role": decision.media_role,
        }
        if len(products) > _MAX_PRODUCT_CHOICES:
            # 候选太多，不展示编号列表；但**必须把原始请求记下来**。
            # 此前这里直接清空状态、什么都不记，于是「发我0653的视频」→反问型号→
            # 员工答「LY0653GDT」时，原始请求（要视频）已经被遗忘，
            # 系统只能把裸型号当成一次全新的「看这个产品」，`_resume_product_choice`
            # 里那套「恢复上一轮意图（含 media_action）」的机制根本没机会生效。
            pending_request["choices_shown"] = False
            self._product_choices[user_id] = pending_request
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
        pending_request["choices_shown"] = True
        self._product_choices[user_id] = pending_request
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
        # 员工写全了型号时，精确命中必须赢过「只是包含它」的变体：否则
        # 「LY0653GDT」会同时匹配 LY0653GDT-T / -Y，于是缩小后仍多于一个候选，
        # 系统继续追问「请先确认具体型号」，而正确答案早就已经明确了。
        exact = [
            item
            for item in products
            if self._product_field_map(item)
            .get("产品型号", item.title)
            .strip()
            .casefold()
            == query.strip().casefold()
        ]
        narrowed = exact or narrow_variants(query, products)
        if index is not None:
            if not pending.get("choices_shown", True):
                # 上一轮没有展示编号列表（候选超过上限），员工不可能在选编号。
                # 不能拿 ids[index] 去猜，那会答出一个他从未见过的产品。
                self._state_pop(self._product_choices, user_id)
                return None
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
        if pending.get("choices_shown", True):
            action_requested = fallback.intent in {Intent.PRODUCT, Intent.PRODUCT_MEDIA}
            intent = fallback.intent if action_requested else saved_intent
            search_query = query if action_requested else str(pending["query"])
        else:
            # 上一轮候选超限、没有展示编号列表，只说了「请补充型号」。员工这一句就是来补型号的：
            # 意图必须沿用上一轮那件事（例如「发我0653的视频」），否则裸型号会被当成一次全新的
            # 「看这个产品」，员工永远拿不到他要的视频或图——这正是生产上出现的现象。
            # 但检索要用**这一句**，因为型号在它里面，上一轮的查询是模糊的。
            action_requested = fallback.media_action is not None
            intent = fallback.intent if action_requested else saved_intent
            search_query = query
        return IntentDecision(
            intent=intent,
            confidence=0.99,
            reason="员工正在确认上一轮的具体产品型号",
            query_for_search=search_query,
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
        # 这里原先还有一条「拼型号进 search_query」的分支，它是不可达的：
        # 进入条件是 has_product_identifier 为假，而下面第 279 行附近的守卫又要求它为真，
        # 且真正的检索用的一直是 original_query，拼好的 search_query 从未被使用。
        # 指代类追问（换成主体图 等）现在由上面的 is_product_follow_up 统一承接。
        # 型号可能来自员工本句，也可能来自计划已解析出的引用（调用方会把它并进
        # search_query）。守卫只看 original_query 会让后者一律落到下面的拒答。
        identifier_in_query = has_product_identifier(original_query)
        if not identifier_in_query and not has_product_identifier(search_query):
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
        actual_query = original_query if identifier_in_query else search_query
        results = self.store.search_products(
            actual_query,
            limit=500,
            query_embedding=None,
        )
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
        target = self._catalog_page_target(query)
        if direction is None and target is None:
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
        next_page = target if target is not None else current_page + direction
        if next_page < 1 or next_page > total_pages:
            if target is not None:
                return RoutedReply(
                    Intent.PRODUCT, f"产品目录只有 {total_pages} 页。", []
                )
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
        # 员工一次点名多个型号时（「A 和 B 哪个贵」），每个引用就是一个具体型号，
        # 精确命中必须优先于「只是包含它」的变体。否则「LY0653GDT」会同时命中
        # LY0653GDT-T / LY0653GDT-Y，一个明确的对比请求被展开成一串同系列产品。
        # 只有一个引用时不启用：那时「LY0653GDT系列有哪些」仍然应当展开家族。
        prefer_exact = len(references) > 1
        for reference in references:
            wanted = reference.strip().casefold()
            exact = (
                [
                    product
                    for product in products
                    if self._product_field_map(product)
                    .get("产品型号", product.title)
                    .strip()
                    .casefold()
                    == wanted
                ]
                if prefer_exact
                else []
            )
            matches = exact or [
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
        reply = self._format_product_catalog(
            products,
            intro=intro,
            display_fields=display_fields,
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
        )
        verdict = (
            self._comparison_verdict(products)
            if product_query.operation == "compare"
            else None
        )
        if verdict:
            # 对比提问的落点是「哪个更贵」，只并列资料等于没回答，因此补一句确定性结论。
            return replace(reply, content=f"{reply.content}\n\n{verdict}")
        return reply

    @staticmethod
    def _price_verdict(priced: list[tuple[str, Decimal]]) -> str | None:
        """用已登记的公开套装价给出确定性对比结论；不足两个有价型号就不下结论。

        两条执行路径（旧链路与 V2）取出价格的方式不同，但结论的措辞和判断必须一致，
        所以判断逻辑放在这里共用，各自只负责把自己的结果整理成 (型号, 价格)。
        """
        priced = [(model, price) for model, price in priced if price is not None]
        if len(priced) < 2:
            return None
        highest = max(priced, key=lambda item: item[1])
        lowest = min(priced, key=lambda item: item[1])
        if highest[1] == lowest[1]:
            return f"对比：以上型号的套装价相同（{highest[1]:.2f}元）。"
        return (
            f"对比：{highest[0]} 的套装价最高（{highest[1]:.2f}元），"
            f"{lowest[0]} 最低（{lowest[1]:.2f}元）。"
        )

    def _comparison_verdict(self, products: list[BusinessResult]) -> str | None:
        """旧链路：从已登记产品的文本字段里取公开套装价。"""
        priced: list[tuple[str, Decimal]] = []
        for product in products:
            fields = self._product_field_map(product)
            model = str(fields.get("产品型号") or product.title)
            match = re.search(r"\d+(?:\.\d+)?", str(fields.get("套装价") or ""))
            if not match:
                continue
            try:
                priced.append((model, Decimal(match.group())))
            except InvalidOperation:
                continue
        return self._price_verdict(priced)

    @staticmethod
    def _v2_comparison_verdict(items: list[dict[str, object]]) -> str | None:
        """V2 链路：价格已经是数值字段，直接用。"""
        priced: list[tuple[str, Decimal]] = []
        for item in items:
            raw = item.get("set_price")
            if raw is None or raw == "":
                continue
            try:
                priced.append((str(item.get("model") or ""), Decimal(str(raw))))
            except InvalidOperation:
                continue
        return ProductToolMixin._price_verdict(priced)

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

    @staticmethod
    def _public_note(value: object, *, can_view_cost: bool) -> str:
        """备注里混着装箱规格和内部信息（供应商、单价），只对外给出装箱规格。

        61 款产品里有 5 款的备注形如「10只装；止水阀*2：供应商圣冠，单价0.27元」。
        装箱规格是客户会问的（几只装），内部单价不是。旧链路按「；」切分渲染后的文本、
        恰好只显示了第一段，属于侥幸；V2 直接读库里的整列，会把单价原样显示给
        没有成本权限的人。所以这里按段过滤：无权限时丢掉含内部信息的段。
        """
        text = str(value or "").strip()
        if not text or can_view_cost:
            return text
        internal = ("单价", "成本", "采购", "进价", "供应商", "毛利")
        kept = [
            part.strip()
            for part in text.split("；")
            if part.strip() and not any(word in part for word in internal)
        ]
        return "；".join(kept)

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
        # 与 _answer_product_v2 同一口径：只在 detail 下追问字段，且"全部参数"不追问（
        # allow_overview=True 时完整资料就是答案）。列表/检索的展示字段由下面按操作给出，
        # 不该因为"模型顺带点的字段被校验剔掉"就拒答——
        # 「镜面玻璃·镜面 3 款是哪三款」就是这么被拒掉的，而它完全答得出来。
        if (
            product_query.operation == "detail"
            and not product_query.allow_overview
            and not requested
        ):
            return RoutedReply(
                Intent.PRODUCT, "无法可靠确认你要查询的字段，请说明想了解哪项产品信息。", []
            )
        full_detail = product_query.operation == "detail" and not requested
        # 默认字段集按**操作**分，不按"恰好匹配到几款"分。
        #
        # 这里原来对所有没有可确认字段的查询都用同一份「完整资料」字段集（15 项、含配件明细）。
        # 完整资料用于单款 detail 是对的；但列表一旦退回它，就会每款吐一份完整明细——
        # 「镜面玻璃·镜面是哪三款」输出了 3 份、近 50 行，款数一多就没法看。
        # 列表/检索这类"有哪些"的提问，默认给能横向比较的几项就够了；
        # 想看配件明细，员工会自己问（"它的配件明细"），那时才走 detail。
        compact_fields = ("model", "series", "color", "material", "set_price")
        complete_fields = (
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
            "notes",
            "supplier",
            "body_supplier",
            "components",
        )
        if requested:
            fields = requested
        elif product_query.operation == "recommend":
            fields = ("model", "series", "material", "color", "special_function", "set_price")
        elif product_query.operation == "detail":
            fields = complete_fields
        else:
            fields = compact_fields
        if "model" not in fields and len(result.items) > 1:
            fields = ("model", *fields)

        total_pages = max(1, (result.total + result.page_size - 1) // result.page_size)
        blocks: list[str] = []
        if result.total > 1:
            blocks.append(
                f"找到 {result.total} 款符合条件的产品，第 {result.page}/{total_pages} 页："
            )
        # 列表默认**每款一行**。
        #
        # 列表是拿来横向比较的（哪款贵、哪款是什么颜色），一行一款才扫得动；
        # 每款铺开成多行会让一页 5 款变成 30 行，款数一多就淹没员工。
        # 员工明确点名了字段时（requested 非空）仍按多行展开——那是他自己要看细节。
        compact_rows = not requested and product_query.operation not in {
            "detail",
            "recommend",
        }
        compact_lines: list[str] = []
        for index, item in enumerate(result.items, (result.page - 1) * result.page_size + 1):
            lines: list[str] = []
            column_by_field = {
                **{name: name for name in PRODUCT_FIELD_CATALOG},
                # 字段名与数据库列名不一致的映射必须写在展开之后。写在前面的会被
                # 同名的 name→name 映射覆盖掉，于是 description 永远取不到
                # product_description，产品描述一直显示「资料未登记」。
                "description": "product_description",
            }
            if compact_rows:
                cells = [str(item.get("model") or "资料未登记")]
                for field in fields:
                    if field in {"model", "images", "videos", "components"}:
                        continue
                    value = item.get(column_by_field[field])
                    if field == "notes":
                        value = self._public_note(value, can_view_cost=can_view_cost)
                    rendered = self._v2_value(field, value)
                    if rendered not in _EMPTY_PRODUCT_FIELD_VALUES:
                        cells.append(rendered)
                lines.append(f"{index}. " + " ｜ ".join(cells))
                # 收进同一个块：逐款 append 会被最后的 "\n\n" 连接符隔出空行，
                # 5 款就多出 5 行空白，紧凑的意义就没了。
                compact_lines.append(lines[0])
                continue
            if result.total > 1:
                lines.append(f"{index}. {item.get('model') or '资料未登记'}")
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
                if field == "notes":
                    value = self._public_note(value, can_view_cost=can_view_cost)
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
        if compact_lines:
            blocks.append("\n".join(compact_lines))
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

    def _v2_catalog_page_reply(
        self,
        user_id: str,
        query: str,
        *,
        can_view_cost: bool,
    ) -> RoutedReply | None:
        """V2 路径下的产品目录翻页。

        与旧链路的 `_catalog_page_reply` 不同：这里不缓存产品 id，也不复用旧格式器，
        而是拿着上次的查询条件与页码，按当前模式再查一次下一页。这样第 1 页和第 2 页
        用的是同一个格式器，不会出现两种排版混在一起。
        """
        context = self._state_get(self._product_catalog_context, user_id)
        if not isinstance(context, dict) or context.get("mode") != "v2":
            return None
        direction = self._catalog_page_direction(query)
        target = self._catalog_page_target(query)
        if direction is None and target is None:
            return None
        try:
            base = product_query_from_dict(dict(context.get("query") or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_page_context_invalid", error_type=type(exc).__name__)
            self._state_pop(self._product_catalog_context, user_id)
            return None
        page_size = base.page_size or _CATALOG_PAGE_SIZE
        total = int(context.get("total") or 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        next_page = target if target is not None else int(context.get("page") or 1) + direction
        if next_page < 1 or next_page > total_pages:
            if target is not None:
                return RoutedReply(Intent.PRODUCT, f"产品目录只有 {total_pages} 页。", [])
            return RoutedReply(
                Intent.PRODUCT,
                "已经是产品目录的第一页。" if direction < 0 else "已经是产品目录的最后一页。",
                [],
            )
        paged = replace(base, page=next_page)
        result = self.store.query_products_v2(paged, include_sensitive=can_view_cost)
        if not result.items:
            self._state_pop(self._product_catalog_context, user_id)
            return RoutedReply(Intent.PRODUCT, "当前产品目录已没有可展示的产品。", [])
        context["page"] = next_page
        self._product_catalog_context[user_id] = context
        return self._format_product_v2(result, paged, can_view_cost=can_view_cost)

    def _v2_aggregate_page_reply(
        self,
        user_id: str,
        query: str,
        *,
        can_view_cost: bool,
    ) -> RoutedReply | None:
        """统计结果的翻页（「一共有多少种颜色」的第 2 页）。

        与产品清单翻页同一套做法：不缓存取值列表，只记住上次的查询条件与页码，
        翻页时用同样条件重算一次。这样库里改了产品，翻到的那页也是新的，
        不会出现"缓存的那几种"和当前库对不上。
        """
        context = self._state_get(self._product_catalog_context, user_id)
        if not isinstance(context, dict) or context.get("mode") != "v2_aggregate":
            return None
        direction = self._catalog_page_direction(query)
        target = self._catalog_page_target(query)
        if direction is None and target is None:
            return None
        try:
            base = product_query_from_dict(dict(context.get("query") or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("aggregate_page_context_invalid", error_type=type(exc).__name__)
            self._state_pop(self._product_catalog_context, user_id)
            return None
        if base is None or base.aggregate is None:
            self._state_pop(self._product_catalog_context, user_id)
            return None
        total_values = int(context.get("total_values") or 0)
        total_pages = max(1, (total_values + _AGGREGATE_PAGE_SIZE - 1) // _AGGREGATE_PAGE_SIZE)
        next_page = target if target is not None else int(context.get("page") or 1) + direction
        if next_page < 1 or next_page > total_pages:
            if target is not None:
                return RoutedReply(Intent.PRODUCT, f"统计结果只有 {total_pages} 页。", [])
            return RoutedReply(
                Intent.PRODUCT,
                "已经是第一页。" if direction < 0 else "已经是最后一页。",
                [],
            )
        reply = self._answer_product_aggregate(base, can_view_cost=can_view_cost, page=next_page)
        if reply is None:
            self._state_pop(self._product_catalog_context, user_id)
            return None
        context["page"] = next_page
        self._product_catalog_context[user_id] = context
        return reply

    def _answer_product_aggregate(
        self, query: ProductQuery, *, can_view_cost: bool, page: int = 1
    ) -> RoutedReply | None:
        """统计类提问：有多少种 / 最大最小 / 平均。

        刻意在**取回结果之后用 Python 统计**，不下推成 SQL 聚合。这样权限过滤、字段语义、
        引用解析全都复用同一条查询路径——否则 `AVG(cost_price)` 之类要在 SQL 里把权限判断
        再实现一遍，漏一次就是越权。代价是要把匹配项都取回来，所以有行数上限。

        `distinct_count` 的取值列表按 `_AGGREGATE_PAGE_SIZE` 分页，和产品清单一样支持
        「下一页」：既不像以前那样截断后只说"另有 N 种"（等于藏了内容），
        也不把几十种取值一次性刷屏。
        """
        aggregate = query.aggregate
        if aggregate is None:
            return None
        definition = PRODUCT_FIELD_CATALOG.get(aggregate.field)
        if definition is None:
            return None

        # validate_product_query 把 page_size 限到 5，统计要覆盖全部匹配项，这里自己放大。
        #
        # 同时必须强制 all_matches：模型对"哪个"这类提问会给 result_scope=first
        # （听起来只要一条），而存储层对 first 会把 page_size 压成 1——
        # 于是"最便宜的是哪个"变成"在当前这 1 条里最便宜"，答案直接错。
        # 统计的口径永远是"全部匹配项"，与"只显示第一条"是两回事。
        result = self.store.query_products_v2(
            replace(
                query,
                page=1,
                page_size=_AGGREGATE_MAX_ROWS,
                result_scope="all_matches",
            ),
            include_sensitive=can_view_cost,
        )
        items = [item for item in result.items if isinstance(item, dict)]
        if not items:
            return RoutedReply(Intent.PRODUCT, "当前没有符合条件的产品，无法统计。", [])
        sources = [
            {"source_type": "product", "source_id": str(item.get("id") or "")}
            for item in items[:20]
            if item.get("id")
        ]
        label = definition.label

        def text_of(item: dict[str, object]) -> str | None:
            raw = item.get(aggregate.field)
            if raw is None or raw == "" or raw == []:
                return None
            if definition.value_type == "number":
                return f"{float(raw):.2f}"
            return str(raw)

        if aggregate.kind == "distinct_count":
            counts: dict[str, int] = {}
            for item in items:
                value = text_of(item)
                if value is None:
                    continue
                counts[value] = counts.get(value, 0) + 1
            if not counts:
                return RoutedReply(
                    Intent.PRODUCT, f"这些产品的{label}都没有登记，无法统计。", [], 
                    execution_trace={},
                )
            ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
            # 标识符不能做"有多少种"：数它等于数产品行数。
            #
            # 以前这条判断靠事后守卫（看"取值数 / 产品数 ≥ 0.9"才拦），现在由**本体声明**决定：
            # `series` / `model` 在 schema.py 里被声明为 identifier，于是构造上就不会统计它。
            # 比例检查保留为兜底 —— 声明说某字段是维度、而数据说它几乎每行都不同时，
            # 说明本体过期了，这时宁可如实说明也不要报个废数字。
            degenerate = len(items) >= 5 and len(ordered) / len(items) >= 0.9
            if not is_aggregatable(aggregate.field) or degenerate:
                return RoutedReply(
                    Intent.PRODUCT,
                    self._identifier_field_message(label, aggregate.field, items, ordered),
                    sources,
                )
            total_pages = max(1, (len(ordered) + _AGGREGATE_PAGE_SIZE - 1) // _AGGREGATE_PAGE_SIZE)
            current = min(max(1, page), total_pages)
            start = (current - 1) * _AGGREGATE_PAGE_SIZE
            shown = ordered[start : start + _AGGREGATE_PAGE_SIZE]
            header = f"当前 {result.total} 款产品一共有 {len(ordered)} 种{label}："
            if total_pages > 1:
                header = (
                    f"当前 {result.total} 款产品一共有 {len(ordered)} 种{label}"
                    f"，第 {current}/{total_pages} 页："
                )
            lines = [header, ""]
            lines.extend(f"{name} {count} 款" for name, count in shown)
            if total_pages > 1:
                if current < total_pages:
                    lines.append('回复"下一页"继续查看。')
                else:
                    lines.append('已经是最后一页。回复"上一页"可以往回看。')
            unregistered = len(items) - sum(counts.values())
            if unregistered and current == total_pages:
                # 只在最后一页提一次，免得每页都重复
                lines.append(f"另有 {unregistered} 款未登记{label}。")
            return RoutedReply(
                Intent.PRODUCT,
                "\n".join(lines),
                sources,
                # 页码信息交给调用方决定要不要记翻页上下文（total_values 为 0 时不记）
                execution_trace={
                    "total_values": len(ordered),
                    "total_pages": total_pages,
                    "page": current,
                },
            )

        numeric = [
            (float(value), str(item.get("model") or ""))
            for item in items
            for value in [item.get(aggregate.field)]
            if value not in (None, "")
        ]
        if not numeric:
            return RoutedReply(
                Intent.PRODUCT, f"这些产品的{label}都没有登记数值，无法统计。", sources
            )
        if aggregate.kind == "average":
            average = sum(value for value, _ in numeric) / len(numeric)
            return RoutedReply(
                Intent.PRODUCT,
                f"当前 {len(numeric)} 款已登记{label}的产品，平均{label}是 {average:.2f}元。",
                sources,
            )
        target = max(numeric) if aggregate.kind == "max" else min(numeric)
        word = "最高" if aggregate.kind == "max" else "最低"
        ties = [model for value, model in numeric if value == target[0]]
        suffix = f"（另有 {len(ties) - 1} 款同价）" if len(ties) > 1 else ""
        return RoutedReply(
            Intent.PRODUCT,
            f"当前 {len(numeric)} 款已登记{label}的产品里，{label}{word}的是 {target[1]}："
            f"{target[0]:.2f}元。{suffix}",
            sources,
        )

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
        # 待澄清的歧义回答由计划层的 pending 槽位在 create_plan 里接回，
        # 因此这里不再读取 V2 上下文里的 pending_ambiguity（那份机制已合并）。
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
            # shadow 运行提出的澄清不会交付给员工，因此不写槽位、也不写正式上下文。
            if not shadow:
                pending.remember_product_query(
                    self,
                    user_id,
                    validation.query,
                    ambiguity.question,
                    option_labels=tuple(ambiguity.options),
                )
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
            # 统计类提问被校验挡下时（例如对文本字段求平均），别回"查询条件无法安全执行"
            # 这种看不懂的话，直接说清能统计什么。
            if product_query.operation == "aggregate":
                return finish(
                    RoutedReply(
                        Intent.PRODUCT,
                        "这个统计暂时做不了。可以统计：颜色、系列、材质这类有多少种，"
                        "以及套装价、主体价、件数这类的高低或平均。",
                        [],
                        execution_trace=trace,
                    ),
                    "clarification",
                    "aggregate_invalid",
                )
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
        # 把筛选值钉到正确的字段上、并归一到库里的写法。
        # 必须在执行之前：模型给的字段可能猜错（同一个颜色被猜成 series / description），
        # 字符串写法也可能对不上（"灰玻璃枪灰" vs "灰玻璃·枪灰"），两者都会静默返回 0 款。
        query = self._resolve_product_filter_values(query)
        if query.operation == "aggregate":
            aggregate_reply = self._answer_product_aggregate(
                query, can_view_cost=can_view_cost
            )
            if aggregate_reply is not None:
                # 记录本次统计，供「下一页」使用。与产品清单同样只记住条件与页码，
                # 翻页时重算，避免缓存的取值列表与当前库脱节。
                # 与清单一致：shadow 是观测，不写正式上下文，否则会盖掉旧链路那份。
                page_info = dict(aggregate_reply.execution_trace or {})
                if not shadow and page_info.get("total_values"):
                    self._product_catalog_context[user_id] = {
                        "mode": "v2_aggregate",
                        "query": product_query_to_dict(query),
                        "page": int(page_info.get("page") or 1),
                        "total_values": int(page_info["total_values"]),
                    }
                return finish(
                    replace(aggregate_reply, execution_trace=trace),
                    "fact",
                    "",
                )
            # 聚合字段被校验丢掉（原话里没有依据）时不要瞎猜，问清楚。
            return finish(
                RoutedReply(
                    Intent.PRODUCT,
                    "你想统计哪个字段？例如颜色、系列、材质有多少种，或套装价的高低、平均。",
                    [],
                    execution_trace=trace,
                ),
                "clarification",
                "aggregate_field_missing",
            )
        # 只在 detail 下追问"你要看哪项信息"，而且**要"全部参数"时不追问**。
        #
        # detail 是"问某个型号的某一项"（如「LY0653GDT 的材质」），字段被剔掉就真的答不了。
        # 但「LY0653GDT 的全部参数」里模型会枚举全部 16 个字段，而"全部参数"不含字段名，
        # 依据校验会把它们全剔掉——这时如果还追问"你要看哪项"，就等于对"全部"这个要求视而不见。
        # 员工说了要全部，完整资料本身就是答案（allow_overview 已由 is_overview_request 判定）。
        #
        # 另外 list / search 的展示字段由渲染固定给出，本来就不该因为"模型顺带点的字段被剔掉"
        # 而拒答——「镜面玻璃·镜面 3 款是哪三款」就是这么被拒掉的。
        if (
            query.operation == "detail"
            and not query.allow_overview
            and not query.effective_field_requests()
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
        # 员工写全型号时，精确命中必须优先于「只是包含它」的变体，否则：
        #   「LY0653GDT 的材质」→ 命中 LY0653GDT / -T / -Y，直接答案退化成一串同系列列表；
        #   「A 和 B 哪个贵」    → 每个型号被展开成家族，两点对比变成一堆产品。
        # 判定很保守：只有当每个引用都精确命中恰好一个产品时才采用，
        # 所以「0653」这类片段写法（精确命中 0 个）仍然走模糊匹配。
        # 清单类（operation == "list"）不适用：「LY0653GDT系列有哪些」本来就要展开家族。
        references = query.effective_references()
        if references and result.total == 0 and all(item.kind == "exact" for item in references):
            # 模型会把「0653」这类系列前缀标成 exact，而它其实是片段：等值匹配查不到任何东西，
            # 于是「0653 和 0689 哪个便宜」答成「没有符合条件的产品」。
            # 精确匹配一无所获时退回片段匹配——旧链路的 mentions_identifier_alias 本来就有
            # 这层兜底，放在这里对两条路径都成立，也不依赖模型把 kind 标对。
            loosened_query = replace(
                query,
                references=tuple(replace(item, kind="fragment") for item in query.references),
            )
            loosened = self.store.query_products_v2(
                loosened_query, include_sensitive=can_view_cost
            )
            if loosened.total:
                query, result = loosened_query, loosened
        elif query.operation != "list" and references and result.total != len(references):
            exact_query = replace(
                query,
                references=tuple(replace(item, kind="exact") for item in query.references),
            )
            exact_result = self.store.query_products_v2(
                exact_query, include_sensitive=can_view_cost
            )
            if exact_result.total == len(references):
                query, result = exact_query, exact_result
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
        if query.operation == "compare":
            # 对比提问的落点是「哪个更贵」，只并列资料等于没回答。
            verdict = self._v2_comparison_verdict(result.items)
            if verdict:
                reply = replace(reply, content=f"{reply.content}\n\n{verdict}")
        # 只要**给员工看了多款产品**就记下上下文，不按操作名判断。
        # 此前要求 operation == "list"，但「0653 的材质」这类片段型号会让模型给出
        # op=detail + 一个片段引用，渲染出来同样是一份 5 款的家族清单；
        # 于是紧接着的「第3页」读不到上下文、落到模型手里去猜（有时反问、有时拒答），
        # 而明确的答案是「产品目录只有 1 页」。翻页判定只对翻页词生效，记下来不干扰别的提问。
        if not shadow and (query.operation == "list" or result.total > 1):
            # 记录本次清单，供「下一页」使用。旧的翻页状态是「缓存全部产品 id」，
            # 那只由旧链路写入，于是 on 模式下翻页判定读不到上下文、直接掉进通用问答。
            # V2 走 SQL 分页，只需记住查询条件与页码，翻页时用同样条件取下一页即可，
            # 也避免缓存 id 与库内容脱节。
            #
            # 必须限定非 shadow：影子比较是观测，它写入的 "v2" 形态上下文会覆盖旧链路
            # 写入的那份（旧的那份带 ids），于是 shadow 下紧接着的「下一页」反而翻不动。
            self._product_catalog_context[user_id] = {
                "mode": "v2",
                "query": product_query_to_dict(query),
                "page": query.page or 1,
                "total": result.total,
            }
        if validation.permission_denied_fields:
            reply = replace(reply, content=f"{reply.content}\n{cost_access_message}")
        if validation.removed_fields and query.operation == "detail":
            # 只在 detail 下提示：员工指定了某个字段、而这个字段没被确认时，
            # 答案确实不完整。列表和统计的字段由渲染固定给出，"有字段没确认"不影响
            # 它们是否回答了问题——那时贴这句只是噪音（生产上就出现过：
            # 一份完整成功的 12 款清单后面跟了一句"另有字段未能可靠确认，请补充说明"）。
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
            # 翻页必须先处理：旧链路在入口就调 _catalog_page_reply，on 路径此前完全没调，
            # 于是「下一页」在 on 下要么不翻页、要么落进通用问答。
            catalog_page = self._v2_catalog_page_reply(
                user_id, original_query, can_view_cost=can_view_cost
            )
            if catalog_page is not None:
                return catalog_page
            # 统计结果的翻页与清单翻页是两套上下文（一个翻产品、一个翻取值），
            # 都先于计划执行处理，免得「下一页」落到模型手里去猜。
            aggregate_page = self._v2_aggregate_page_reply(
                user_id, original_query, can_view_cost=can_view_cost
            )
            if aggregate_page is not None:
                return aggregate_page
            # 规则构造的筛选、模糊检索和推荐不可信，一律拒绝执行。
            # 但「对比」只要引用了两个明确型号就是确定性操作，规则也能安全构造；
            # 少于两个引用时才算「规则猜出来的对比」，仍然拒绝。
            # 这一条曾经把 on 模式下的对比提问全部拒掉，而 shadow 下它们本来是能答的。
            unsafe_fallback = decision.source not in {
                "planner_model",
                "product_query_context",
                "conversation_state",
            } and (
                bool(product_query.filters)
                or product_query.operation in {"search", "recommend"}
                or (
                    product_query.operation == "compare"
                    and len(product_query.effective_references()) < 2
                )
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
            if not callable(getattr(self.store, "query_products_v2", None)):
                # The store cannot compile V2 queries at all (offline fixtures), so
                # there is nothing to compare against. Record the skip explicitly
                # rather than raising and logging an error on every product request.
                self._state_pop(self._product_shadow_query_context, user_id)
                return replace(
                    legacy,
                    execution_trace={
                        **legacy.execution_trace,
                        "planner_version": "product-query-v2",
                        "shadow_diff": {"skipped": "store_without_query_products_v2"},
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
            # Shadow comparison is observational: the employee still receives the
            # legacy answer, so this must not fail the request. Record the message
            # as well as the type — a bare error_type makes a silently skipped
            # comparison (a store without query_products_v2, for example) very hard
            # to diagnose from logs alone.
            logger.warning(
                "product_query_v2_shadow_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
                exc_info=True,
            )
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
