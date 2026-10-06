"""Product catalog, variant selection and product-data formatting."""

from typing import Any

import structlog

from app.business.intent import Intent, IntentDecision, MediaAction, is_product_catalog_query
from app.business.product_selection import (
    choice_index,
    has_product_identifier,
    is_product_follow_up,
    mentions_identifier,
    narrow_variants,
    product_fields,
)
from app.business.store import BusinessResult
from app.business.tools.common import RoutedReply

logger = structlog.get_logger()


class ProductToolMixin:
    """Product methods mixed into the public BusinessAgent."""

    @staticmethod
    def _product_field_map(product: BusinessResult) -> dict[str, str]:
        return product_fields(product.content)

    def _clear_product_context(self, user_id: str) -> None:
        for state in (self._last_product_by_user, self._last_media_by_user, self._product_choices):
            self._state_pop(state, user_id)

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
        visible = products[:20]
        self._product_choices[user_id] = {
            "ids": [item.source_id for item in visible],
            "query": query,
            "intent": str(decision.intent),
            "media_action": str(decision.media_action) if decision.media_action else None,
            "media_type": decision.media_type,
            "media_role": decision.media_role,
        }
        lines = [prefix or f"找到 {len(products)} 款相关产品，请先确认具体款式："]
        for index, product in enumerate(visible, 1):
            fields = self._product_field_map(product)
            details = "；".join(
                f"{label}：{fields[key][:100]}"
                for key, label in (
                    ("颜色", "颜色"),
                    ("材质", "材质"),
                    ("款式", "款式"),
                    ("档位", "档位"),
                    ("套装价", "套装价"),
                    ("主体价", "主体价"),
                )
                if fields.get(key) and fields[key] not in {"无", "未登记"}
            )
            lines.append(f"{index}. {product.title}" + (f"｜{details}" if details else ""))
        if len(products) > len(visible):
            lines.append("这里先列出前 20 款；其他款式请补充颜色、材质或款式重新查询。")
        lines.append("请回复序号（如“第二个”），或补充颜色、材质、款式。")
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
            reason="员工正在确认上一轮的具体产品款式",
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
                    prefix="当前选项中没有找到该款，请重新选择："
                    if pending.get("selection_error")
                    else "请先确认具体款式：",
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
            return None, self._ask_product_choice(user_id, results, actual_query, decision)
        self._remember_product(user_id, results[0].source_id)
        return results[0], None

    @staticmethod
    def _media_summary(media: list[BusinessResult]) -> tuple[str, str]:
        image_labels = {
            "main_image": "主图",
            "gallery_image": "实物图",
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

    def _component_summary(
        self,
        product: BusinessResult,
        *,
        can_view_cost: bool = False,
        record: dict[str, object] | None = None,
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
        for index, item in enumerate(components, 1):
            details = [str(item.get("name") or "未命名配件")]
            supplier = str(item.get("supplier") or "").strip()
            if supplier:
                details.append(f"供应商：{supplier}")
            if can_view_cost and item.get("unit_price") is not None:
                details.append(f"采购价：{float(item['unit_price']):g}元")
            lines.append(f"{index}. " + "｜".join(details))
        return "\n".join(lines)

    def _format_product_detail(
        self,
        product: BusinessResult,
        media: list[BusinessResult],
        query: str = "",
        *,
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
        component_summary = self._component_summary(
            product, can_view_cost=can_view_cost, record=record
        )
        normalized = query.lower()
        component_price_query = any(marker in normalized for marker in ("采购价", "进价")) or (
            "配件" in normalized
            and any(marker in normalized for marker in ("价格", "多少钱", "单价"))
        )
        sensitive_price_query = component_price_query or any(
            marker in normalized for marker in ("成本价", "成本")
        )
        sensitive_content = ""
        if sensitive_price_query:
            if not can_view_cost:
                sensitive_content = cost_access_message
            elif component_price_query:
                sensitive_content = component_summary
            else:
                cost_price = (record or {}).get("cost_price")
                sensitive_content = (
                    f"成本价：{float(cost_price):.2f}元"
                    if cost_price is not None
                    else "成本价：资料中未登记"
                )
        field_queries = (
            ("主体价", "主体多少钱"),
            ("套装价", "价格", "多少钱", "报价", "售价"),
            ("材质",),
            ("档位", "几档"),
            ("颜色",),
            ("款式",),
            ("图片", "照片", "主图", "实物图", "配置图"),
            ("视频",),
            ("配件", "包含什么", "都有什么"),
        )
        matched: list[str] = []
        for markers in field_queries:
            price_markers = {"价格", "多少钱", "报价", "售价"}
            matches = (
                [
                    marker
                    for marker in markers
                    if marker not in price_markers or "主体" not in normalized
                ]
                if markers[0] == "套装价"
                else list(markers)
            )
            if any(marker in normalized for marker in matches):
                if "视频" in markers:
                    label, content = "视频", video_summary
                elif "图片" in markers:
                    label, content = "图片", image_summary
                elif "配件" in markers:
                    label, content = "配件", component_summary
                else:
                    label = markers[0]
                    key = "主体价" if label == "主体价" else label
                    content = f"{label}：{fields.get(key, '资料中未登记')}"
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
            ("款式", "款式"),
            ("材质", "材质"),
            ("档位", "档位"),
            ("颜色", "颜色"),
            ("特殊功能", "特殊功能"),
            ("出货地", "出货地"),
            ("套装价", "套装价"),
            ("主体价", "主体价"),
            ("供应商", "供应商"),
            ("主体供应商", "主体供应商"),
            ("备注", "备注"),
            ("价格口径", "价格口径"),
        )
        lines = [f"{label}：{fields[key]}" for key, label in ordered if fields.get(key)]
        cost_price = (record or {}).get("cost_price")
        if can_view_cost and cost_price is not None:
            lines.append(f"成本价：{float(cost_price):.2f}元")
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
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
    ) -> RoutedReply:
        if not products:
            return RoutedReply(Intent.PRODUCT, "当前没有已发布产品。", [])
        suffix = "（最多展示 500 款）" if len(products) >= 500 else ""
        blocks: list[str] = [f"已发布产品共展示 {len(products)} 款{suffix}："]
        sources: list[dict[str, object]] = []
        for index, product in enumerate(products, 1):
            media = self.store.media_for_product(product.source_id)
            detail = self._format_product_detail(
                product, media, can_view_cost=can_view_cost, cost_access_message=cost_access_message
            ).content
            blocks.append(f"\n【产品{index}】\n{detail}")
            sources.extend(self.store.as_dict(item) for item in [product, *media])
        return RoutedReply(Intent.PRODUCT, "\n".join(blocks), sources)

    def _answer_product(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        prefetched_embedding: list[float] | None = None,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
        **_: object,
    ) -> RoutedReply:
        if is_product_catalog_query(original_query) or is_product_catalog_query(search_query):
            products = self.store.list_active_products(limit=500)
            self._clear_product_context(user_id)
            if len(products) == 1:
                self._remember_product(user_id, products[0].source_id)
            return self._format_product_catalog(
                products, can_view_cost=can_view_cost, cost_access_message=cost_access_message
            )
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
                user_id, original_query, search_query, decision, embedding
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
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
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
