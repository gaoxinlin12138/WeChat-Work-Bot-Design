"""Product image/video lookup boundary; transport stays in app.wecom."""

from dataclasses import replace
from typing import Any

import structlog

from app.business.intent import IntentDecision, MediaAction
from app.business.planner import ProductQuery
from app.business.product_selection import has_product_identifier
from app.business.tools.common import RoutedReply

logger = structlog.get_logger()


class MediaToolMixin:
    def _answer_media(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        product_query: ProductQuery | None = None,
        **kwargs: object,
    ) -> RoutedReply:
        if product_query is not None and product_query.filters:
            # Media lookup has no right to broaden or ignore a structured
            # product predicate.  Route mixed requests through the same
            # parameterized product executor used by product_query.
            return self._answer_product_v2(
                user_id,
                original_query,
                replace(product_query, operation="list"),
                can_view_cost=bool(kwargs.get("can_view_cost", False)),
                cost_access_message=str(
                    kwargs.get("cost_access_message", "你当前没有查看成本价和采购价的权限。")
                ),
            )
        if product_query is not None and product_query.include_models:
            search_query = " ".join((*product_query.include_models, search_query))
        action = decision.media_action or MediaAction.LIST
        media_type = decision.media_type
        media_role = decision.media_role
        if media_type is None and self._state_contains(self._last_media_by_user, user_id):
            media_type, media_role = self._state_get(self._last_media_by_user, user_id)
            action = MediaAction.SEND
        elif media_type is not None:
            self._last_media_by_user[user_id] = (media_type, media_role)
            try:
                if len(self._last_media_by_user) > 500:
                    self._state_pop(self._last_media_by_user, next(iter(self._last_media_by_user)))
            except Exception as exc:
                logger.warning("media_context_cleanup_failed", error_type=type(exc).__name__)
        global_scope = (
            action in (MediaAction.COUNT, MediaAction.LIST)
            and any(
                word in search_query
                for word in ("一共", "总共", "全部", "所有", "产品库", "我们现在")
            )
            and not has_product_identifier(original_query)
            and not (product_query and product_query.include_models)
        )
        related_product_id = None
        product = None
        try:
            if not global_scope:
                product, clarification = self._resolve_product(
                    user_id, original_query, search_query, decision
                )
                if clarification:
                    return clarification
                related_product_id = product.source_id
            self._state_pop(self._pending_media_by_user, user_id)
            results = self.store.search_media(
                search_query,
                media_type,
                limit=100 if action in (MediaAction.COUNT, MediaAction.LIST) else 10,
                related_product_id=related_product_id,
                media_role=media_role,
                include_all=global_scope,
            )
        except Exception as exc:
            logger.error("media_search_failed", error_type=type(exc).__name__)
            return RoutedReply(decision.intent, "产品媒体查询暂时不可用，请稍后重试。", [], action)
        noun = (
            "安装视频"
            if media_role == "product_video" or media_type == "video"
            else "主体图"
            if media_role == "body_image"
            else "产品图"
            if media_role == "product_image"
            else "产品图片"
        )
        scope = product.title if related_product_id and product else "已发布产品库"
        sources = [self.store.as_dict(item) for item in results]
        if action == MediaAction.COUNT:
            return RoutedReply(
                decision.intent, f"{scope}共有 {len(results)} 项{noun}。", sources, action
            )
        if action == MediaAction.LIST:
            if not results:
                return RoutedReply(decision.intent, f"{scope}还没有登记{noun}。", [], action)
            names = "\n".join(
                f"{index}. {item.title}" for index, item in enumerate(results, start=1)
            )
            return RoutedReply(
                decision.intent,
                f"{scope}共有 {len(results)} 项{noun}：\n{names}\n\n如需查看，请回复“发给我”。",
                sources,
                action,
            )
        if not results:
            return RoutedReply(decision.intent, f"{scope}还没有登记{noun}。", [], action)
        names = "、".join(item.title for item in results)
        return RoutedReply(
            decision.intent,
            f"{scope}已找到 {len(results)} 项{noun}，正在发送：{names}。",
            sources,
            action,
        )


class MediaTool(MediaToolMixin):
    """Internal media lookup boundary; WeCom upload/send is elsewhere."""

    name = "media"

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner._answer_media(*args, **kwargs)
