import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import structlog

from app.agent import get_rag_agent as _get_rag_agent
from app.business.intent import decide_intent as _decide_intent
from app.business.session import ReplySessionState, SharedState
from app.business.store import BusinessStore
from app.business.tools import BusinessTools
from app.business.tools.common import RoutedReply
from app.business.tools.knowledge import KnowledgeToolMixin
from app.business.tools.media import MediaToolMixin
from app.business.tools.product import ProductToolMixin
from app.business.tools.reply_script import ReplyScriptToolMixin
from app.config import get_settings

logger = structlog.get_logger()


_ROUTING_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="business-route")


def get_rag_agent():
    """Late-patchable RAG dependency used by graph nodes and evaluations."""
    return _get_rag_agent()


def decide_intent(*args, **kwargs):
    """Late-patchable intent dependency used by graph nodes and evaluations."""
    return _decide_intent(*args, **kwargs)


class BusinessAgent(KnowledgeToolMixin, ProductToolMixin, MediaToolMixin, ReplyScriptToolMixin):
    def __init__(self, store: BusinessStore) -> None:
        self.store = store
        # Keep this alongside the other conversation state so worker processes
        # share context and the 30 minute state TTL applies consistently.
        self._last_product_by_user = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:last-product"
        )
        self._last_media_by_user = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:last-media"
        )
        self._pending_media_by_user = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:pending-media"
        )
        self._reply_sessions = ReplySessionState(
            getattr(get_settings(), "redis_url", None), "wecom:reply-session"
        )
        self._intent_context = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:intent-context"
        )
        self._product_choices = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:product-choices"
        )
        self._product_catalog_context = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:product-catalog"
        )
        self._product_query_context = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:product-query-v2-validated"
        )
        # Single source of truth for product continuation, ordinal selection,
        # pagination and result ordering.  The older maps remain readable for
        # one-time migration and compatibility with in-flight workers.
        self._product_focus = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:product-focus"
        )
        self._knowledge_choices = SharedState(
            getattr(get_settings(), "redis_url", None), "wecom:knowledge-choices"
        )
        # 待澄清槽位：澄清分支不执行任务，所以由它负责把已确定的信息留到下一轮。
        from app.business.pending import STATE_ATTR

        setattr(
            self,
            STATE_ATTR,
            SharedState(
                getattr(get_settings(), "redis_url", None), "wecom:pending-clarification"
            ),
        )
        # Internal domain boundaries. These are not model-facing tools or API
        # endpoints; they keep orchestration separate from domain work.
        self.tools = BusinessTools.for_owner(self)
        self._business_graph = None
        self._checkpoint_resources = None

    def _remember_product(self, user_id: str, product_id: str) -> None:
        try:
            self._last_product_by_user[user_id] = product_id
            if len(self._last_product_by_user) > 500:
                self._last_product_by_user.pop(next(iter(self._last_product_by_user)), None)
        except Exception as exc:
            logger.warning("business_product_context_write_failed", error_type=type(exc).__name__)

    @staticmethod
    def _state_get(state: SharedState, key: str, default: object = None) -> object:
        try:
            return state[key]
        except KeyError:
            return default
        except Exception as exc:
            logger.warning("business_state_read_failed", error_type=type(exc).__name__)
            return default

    @staticmethod
    def _state_contains(state: SharedState, key: str) -> bool:
        try:
            return key in state
        except Exception as exc:
            logger.warning("business_state_membership_failed", error_type=type(exc).__name__)
            return False

    @staticmethod
    def _state_pop(state: SharedState, key: str) -> object | None:
        try:
            return state.pop(key, None)
        except Exception as exc:
            logger.warning("business_state_delete_failed", error_type=type(exc).__name__)
            return None

    def _recent_intent_context(self, user_id: str) -> list[dict[str, str]]:
        try:
            value = self._state_get(self._intent_context, user_id, [])
            return value if isinstance(value, list) else []
        except Exception as exc:
            logger.warning("intent_context_read_failed", error_type=type(exc).__name__)
            return []

    def _record_intent_turn(
        self,
        user_id: str,
        query: str,
        reply: RoutedReply,
        request_id: str | None = None,
    ) -> None:
        context = self._recent_intent_context(user_id)
        if request_id and any(item.get("request_id") == request_id for item in context):
            return
        user_turn = {"role": "user", "content": query[:500], "intent": str(reply.intent)}
        assistant_turn = {
            "role": "assistant",
            "content": reply.content[:500],
            "intent": str(reply.intent),
        }
        if request_id:
            user_turn["request_id"] = request_id
            assistant_turn["request_id"] = request_id
        context.extend([user_turn, assistant_turn])
        try:
            self._intent_context[user_id] = context[-8:]
        except Exception as exc:
            logger.warning("intent_context_write_failed", error_type=type(exc).__name__)

    def _record_intent_audit(
        self,
        *,
        user_id: str,
        query: str,
        reply: RoutedReply,
        response_ms: float,
        request_id: str | None = None,
    ) -> None:
        source_types = sorted(
            {
                str(source.get("source_type") or "")
                for source in reply.sources
                if source.get("source_type")
            }
        )
        recorder = getattr(self.store, "record_intent_audit", None)
        if recorder is None:
            return
        recorder(
            user_id=user_id,
            query=query,
            intent=str(reply.intent),
            confidence=reply.intent_confidence,
            intent_source=reply.intent_source,
            intent_reason=reply.intent_reason,
            understood_query=reply.understood_query,
            data_source=",".join(source_types) or "none",
            response_preview=reply.content[:800],
            source_count=len(reply.sources),
            response_ms=response_ms,
            request_id=request_id,
            planner_version=str(reply.execution_trace.get("planner_version") or ""),
            query_plan_redacted=reply.execution_trace.get("query_plan", {}),
            validation_result=reply.execution_trace.get("validation", {}),
            execution_summary=reply.execution_trace.get("execution", {}),
        )

    def _get_business_graph(self):
        if self._business_graph is None:
            from app.business.graph import build_business_graph

            settings = get_settings()
            checkpointer = None
            if getattr(settings, "langgraph_checkpoint_enabled", False):
                if not getattr(settings, "database_url", None):
                    raise RuntimeError("启用 LangGraph Checkpoint 前必须配置 DATABASE_URL")
                from app.business.graph.checkpoint import create_postgres_checkpointer

                self._checkpoint_resources = create_postgres_checkpointer(
                    settings.database_url,
                    pool_size=getattr(settings, "langgraph_checkpoint_pool_size", 4),
                )
                checkpointer = self._checkpoint_resources.saver
            self._business_graph = build_business_graph(checkpointer=checkpointer)
        return self._business_graph

    def close(self) -> None:
        if self._checkpoint_resources is not None:
            self._checkpoint_resources.close()
            self._checkpoint_resources = None
        self._business_graph = None

    def answer(
        self,
        user_id: str,
        query: str,
        stream_callback: Callable[[str], None] | None = None,
        *,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> RoutedReply:
        settings = get_settings()
        from app.business.graph.identity import build_request_id

        canonical_request_id = build_request_id(request_id) if request_id else None
        from app.business.graph import BusinessGraphContext
        from app.business.graph.identity import build_thread_id
        from app.business.graph.state import reply_from_payload

        graph_thread_id = build_thread_id(
            thread_id or user_id,
            corp_id=getattr(settings, "wecom_corp_id", None),
            bot_id=getattr(settings, "wecom_bot_id", None),
        )
        graph_request_id = canonical_request_id or build_request_id(None)
        result = self._get_business_graph().invoke(
            {"request_id": graph_request_id, "query": query},
            config={
                "configurable": {
                    "thread_id": graph_thread_id,
                }
            },
            context=BusinessGraphContext(
                agent=self,
                user_id=user_id,
                stream_callback=stream_callback,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
            ),
        )
        return reply_from_payload(result["reply"])

    def answer_streaming(
        self,
        user_id: str,
        query: str,
        stream_callback: Callable[[str], None],
        *,
        can_view_cost: bool = False,
        cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> RoutedReply:
        """企业微信入口：模型生成时把经过清理的累计正文交给流式消息层。"""
        return self.answer(
            user_id,
            query,
            stream_callback=stream_callback,
            can_view_cost=can_view_cost,
            cost_access_message=cost_access_message,
            thread_id=thread_id,
            request_id=request_id,
        )


@lru_cache
def get_business_store() -> BusinessStore:
    settings = get_settings()
    if settings.app_env == "test" and os.environ.get("CODEX_TEST_ALLOW_APP_DATABASE") != "1":
        raise RuntimeError("测试环境禁止通过应用工厂连接数据库；请使用 isolated_database_url")
    database_url = settings.database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL 尚未配置")
    store = BusinessStore(database_url, settings.embedding_dimensions)
    store.initialize()
    return store


@lru_cache
def get_business_agent() -> BusinessAgent:
    return BusinessAgent(get_business_store())
