from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.business.graph import build_business_graph
from app.business.graph.builder import _reply_task_query
from app.business.graph.identity import build_request_id, build_thread_id
from app.business.graph.reply_script import get_reply_script_graph
from app.business.graph.state import reply_to_payload
from app.business.intent import Intent, IntentDecision, MediaAction, ReplyScenario
from app.business.service import BusinessAgent
from app.business.tools.common import RoutedReply
from app.messages import WELCOME_MESSAGE


def test_langgraph_path_preserves_reply_and_runtime_context(monkeypatch) -> None:
    class Store:
        def record_intent_audit(self, **_kwargs) -> None:
            pass

    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    agent = BusinessAgent(Store())
    calls = []

    def callback(_content: str) -> None:
        pass

    def media_answer(**kwargs):
        calls.append(kwargs)
        return RoutedReply(
            intent=Intent.PRODUCT_MEDIA,
            content="已找到产品视频。",
            sources=[{"id": "media-1"}],
            media_action=MediaAction.SEND,
            scenario=ReplyScenario.AFTER_SALES_COMPLAINT,
            missing_fields=["客户名称"],
            risk_warnings=["需要审批"],
        )

    agent.tools = SimpleNamespace(
        media=SimpleNamespace(answer=media_answer),
        audit=SimpleNamespace(record_audit=lambda **_kwargs: None),
    )

    actual = agent.answer(
        "employee-1",
        "  发送   0689 视频  ",
        stream_callback=callback,
        can_view_cost=True,
        cost_access_message="无权限",
    )

    assert actual.intent == Intent.PRODUCT_MEDIA
    assert actual.content == "已找到产品视频。"
    assert actual.media_action == MediaAction.SEND
    assert calls[0]["user_id"] == "employee-1"
    assert calls[0]["original_query"] == "发送 0689 视频"
    assert calls[0]["stream_callback"] is callback
    assert calls[0]["can_view_cost"] is True
    assert calls[0]["cost_access_message"] == "无权限"
    assert set(agent._get_business_graph().get_graph().nodes) == {
        "__start__",
        "normalize_input",
        "create_plan",
        "clarification",
        "execute_task",
        "verify_results",
        "finalize_reply",
        "__end__",
    }

def test_graph_runs_real_conversation_workflow(monkeypatch) -> None:
    class Store:
        def __init__(self) -> None:
            self.audits = []

        def record_intent_audit(self, **kwargs) -> None:
            self.audits.append(kwargs)

    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    store = Store()
    agent = BusinessAgent(store)

    reply = agent.answer("employee-graph", "  你好  ")

    assert reply.intent == Intent.CONVERSATION
    assert reply.content == WELCOME_MESSAGE
    assert store.audits[0]["user_id"] == "employee-graph"
    assert store.audits[0]["query"] == "你好"


def test_graph_executes_and_merges_compound_plan(monkeypatch) -> None:
    class Store:
        def record_intent_audit(self, **_kwargs) -> None:
            pass

    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    agent = BusinessAgent(Store())
    calls = []

    def product_answer(**kwargs):
        calls.append(("product", kwargs["search_query"]))
        return RoutedReply(
            Intent.PRODUCT,
            "套装价：170.00元",
            [{"source_type": "product", "source_id": "product-1"}],
        )

    def media_answer(**kwargs):
        calls.append(("media", kwargs["search_query"]))
        return RoutedReply(
            Intent.PRODUCT_MEDIA,
            "已找到安装视频，正在发送。",
            [
                {
                    "source_type": "media",
                    "source_id": "media-1",
                    "media_type": "video",
                    "media_path": "install.mp4",
                }
            ],
            media_action=MediaAction.SEND,
        )

    agent.tools = SimpleNamespace(
        product=SimpleNamespace(answer=product_answer),
        media=SimpleNamespace(answer=media_answer),
        audit=SimpleNamespace(record_audit=lambda **_kwargs: None),
    )

    reply = agent.answer("employee-plan", "LTA-822多少钱，再把安装视频发我")

    assert [name for name, _query in calls] == ["product", "media"]
    assert reply.intent == Intent.PRODUCT_MEDIA
    assert reply.media_action == MediaAction.SEND
    assert "【产品信息】" in reply.content
    assert "【产品媒体】" in reply.content
    assert {source["source_id"] for source in reply.sources} == {"product-1", "media-1"}


def test_graph_verifies_inventory_requirement(monkeypatch) -> None:
    class Store:
        def record_intent_audit(self, **_kwargs) -> None:
            pass

    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    agent = BusinessAgent(Store())
    agent.tools = SimpleNamespace(
        product=SimpleNamespace(
            answer=lambda **_kwargs: RoutedReply(
                Intent.PRODUCT,
                "件数：15件",
                [{"source_type": "product", "source_id": "product-1"}],
            )
        ),
        audit=SimpleNamespace(record_audit=lambda **_kwargs: None),
    )

    reply = agent.answer("employee-stock", "LTA-822库存够不够20套")

    assert "不足20套" in reply.content
    assert "还差5件" in reply.content


def test_reply_task_evidence_excludes_sensitive_prices() -> None:
    state = {
        "task_replies": [
            reply_to_payload(
                RoutedReply(
                    Intent.PRODUCT,
                    "套装价：170.00元\n成本价：90.00元\n配件采购价：12.00元",
                    [],
                )
            )
        ]
    }
    decision = IntentDecision(
        Intent.REPLY_SCRIPT,
        0.95,
        "生成客户回复",
        "帮我写报价回复",
        reply_scenario=ReplyScenario.PRICE_NEGOTIATION,
    )

    query = _reply_task_query(state, decision)

    assert "套装价：170.00元" in query
    assert "成本价" not in query
    assert "采购价" not in query


def test_graph_runs_multi_turn_reply_workflow(monkeypatch) -> None:
    class Store:
        def __init__(self) -> None:
            self.audits = []

        def search_scripts(self, *_args, **_kwargs):
            return []

        def search_products(self, *_args, **_kwargs):
            return []

        def as_dict(self, item):
            return {"title": item.title}

        def record_intent_audit(self, **kwargs) -> None:
            self.audits.append(kwargs)

    settings = SimpleNamespace(
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.business.service.get_rag_agent",
        lambda: SimpleNamespace(search=lambda *_args, **_kwargs: []),
    )
    agent = BusinessAgent(Store())
    queries = ["客户觉得价格太高，想再便宜一点", "A-100，50套，当前报价800元"]

    replies = [agent.answer("graph-user", query) for query in queries]

    assert replies[0].scenario == ReplyScenario.PRICE_NEGOTIATION
    assert "产品名称或型号" in replies[0].content
    assert replies[-1].scenario == ReplyScenario.PRICE_NEGOTIATION
    assert "直接回复版" in replies[-1].content


def test_reply_script_subgraph_has_collection_and_generation_steps() -> None:
    assert set(get_reply_script_graph().get_graph().nodes) == {
        "__start__",
        "load_session",
        "collect_fields",
        "generate_reply",
        "__end__",
    }


def test_graph_identifiers_are_stable_scoped_and_privacy_safe() -> None:
    first = build_thread_id("employee-1", corp_id="corp-a", bot_id="bot-a")
    repeated = build_thread_id("employee-1", corp_id="corp-a", bot_id="bot-a")
    other_bot = build_thread_id("employee-1", corp_id="corp-a", bot_id="bot-b")
    request = build_request_id("message-123")

    assert first == repeated
    assert first != other_bot
    assert "employee-1" not in first
    assert "message-123" not in request
    assert build_request_id("message-123") == request
    assert build_request_id(None) != build_request_id(None)


def test_checkpointed_graph_uses_scoped_thread_and_request_ids(monkeypatch) -> None:
    class Store:
        def record_intent_audit(self, **_kwargs) -> None:
            pass

    saver = InMemorySaver()
    settings = SimpleNamespace(
        langgraph_checkpoint_enabled=True,
        langgraph_checkpoint_pool_size=2,
        database_url="postgresql://unused/test",
        wecom_corp_id="corp-a",
        wecom_bot_id="bot-a",
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.business.graph.checkpoint.create_postgres_checkpointer",
        lambda *_args, **_kwargs: SimpleNamespace(saver=saver),
    )
    agent = BusinessAgent(Store())

    reply = agent.answer(
        "employee-1",
        "你好",
        thread_id="wecom:direct:employee-1",
        request_id="message-123",
    )

    thread_id = build_thread_id(
        "wecom:direct:employee-1", corp_id="corp-a", bot_id="bot-a"
    )
    snapshot = agent._business_graph.get_state(
        {
            "configurable": {
                "thread_id": thread_id,
            }
        }
    )
    assert reply.intent == Intent.CONVERSATION
    assert snapshot.values["request_id"] == build_request_id("message-123")
    assert "user_id" not in snapshot.values


def test_checkpoint_requires_database_url(monkeypatch) -> None:
    agent = BusinessAgent.__new__(BusinessAgent)
    agent._business_graph = None
    agent._checkpoint_resources = None
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(langgraph_checkpoint_enabled=True, database_url=None),
    )

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        agent._get_business_graph()


def test_request_id_prevents_duplicate_intent_context(monkeypatch) -> None:
    settings = SimpleNamespace(redis_url=None)
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    agent = BusinessAgent(SimpleNamespace())
    reply = RoutedReply(Intent.CONVERSATION, "你好", [])

    agent._record_intent_turn("employee-1", "你好", reply, request_id="request-1")
    agent._record_intent_turn("employee-1", "你好", reply, request_id="request-1")

    assert len(agent._recent_intent_context("employee-1")) == 2


def test_new_graph_instance_recovers_existing_thread_checkpoint(monkeypatch) -> None:
    class Store:
        def record_intent_audit(self, **_kwargs) -> None:
            pass

    settings = SimpleNamespace(
        langgraph_checkpoint_enabled=False,
        wecom_corp_id="corp-a",
        wecom_bot_id="bot-a",
        redis_url=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        task_planner_enabled=False,
        intent_confidence_threshold=0.45,
    )
    monkeypatch.setattr("app.business.service.get_settings", lambda: settings)
    saver = InMemorySaver()
    first_agent = BusinessAgent(Store())
    first_agent._business_graph = build_business_graph(checkpointer=saver)
    first_agent.answer(
        "employee-1",
        "你好",
        thread_id="direct:employee-1",
        request_id="message-1",
    )

    restarted_agent = BusinessAgent(Store())
    restarted_agent._business_graph = build_business_graph(checkpointer=saver)
    thread_id = build_thread_id(
        "direct:employee-1", corp_id="corp-a", bot_id="bot-a"
    )
    snapshot = restarted_agent._business_graph.get_state(
        {"configurable": {"thread_id": thread_id}}
    )

    assert snapshot.values["request_id"] == build_request_id("message-1")
    assert snapshot.values["reply"]["intent"] == str(Intent.CONVERSATION)
