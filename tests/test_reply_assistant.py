import json
from types import SimpleNamespace

from app.business.intent import (
    Intent,
    IntentDecision,
    ReplyScenario,
    classify_intent,
    classify_reply_scenario,
)
from app.business.service import BusinessAgent


class EmptyStore:
    def search_scripts(self, *_args, **_kwargs):
        return []

    def search_products(self, *_args, **_kwargs):
        return []

    def as_dict(self, item):
        return {"title": item.title}


class FakeRedis:
    data = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def delete(self, key):
        return int(self.data.pop(key, None) is not None)

    def scan_iter(self, pattern):
        prefix = pattern.removesuffix("*")
        return (key for key in self.data if key.startswith(prefix))

    def dbsize(self):
        return len(self.data)


def test_customer_messages_route_to_reply_assistant():
    assert classify_intent("客户觉得价格太高，想再便宜一点") == Intent.REPLY_SCRIPT
    assert (
        classify_reply_scenario("客户觉得价格太高，想再便宜一点") == ReplyScenario.PRICE_NEGOTIATION
    )


def test_reply_assistant_collects_missing_fields(monkeypatch):
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(llm_api_key=None, llm_model=None),
    )
    agent = BusinessAgent(EmptyStore())
    first = agent.answer("employee-1", "客户觉得价格太高，想再便宜一点")
    assert first.scenario == ReplyScenario.PRICE_NEGOTIATION
    assert "产品名称或型号" in first.content
    second = agent.answer("employee-1", "A-100，50套，当前报价800元")
    assert second.scenario == ReplyScenario.PRICE_NEGOTIATION
    assert "直接回复版" in second.content
    assert "风险提示" in second.content


def test_reply_assistant_can_reset_session(monkeypatch):
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(llm_api_key=None, llm_model=None),
    )
    agent = BusinessAgent(EmptyStore())
    agent.answer("employee-1", "客户问交期怎么回复")
    reset = agent.answer("employee-1", "重置")
    assert "清除" in reset.content
    assert "employee-1" not in agent._reply_sessions


def test_reply_follow_up_does_not_switch_price_scenario(monkeypatch):
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(llm_api_key=None, llm_model=None),
    )
    agent = BusinessAgent(EmptyStore())
    agent.answer("employee-1", "客户觉得价格太高，想再便宜一点")
    reply = agent.answer("employee-1", "0689产品，数量是10套，276")
    assert reply.scenario == ReplyScenario.PRICE_NEGOTIATION
    assert "直接回复版" in reply.content


def test_after_sales_supplements_survive_product_or_general_misroute(monkeypatch):
    """模型把补充句误判为产品/通用资料时，不能丢掉售后会话。"""
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(
            llm_api_key=None,
            llm_model=None,
            redis_url=None,
            embedding_api_key=None,
            embedding_model=None,
            task_planner_enabled=False,
            intent_confidence_threshold=0.45,
        ),
    )
    agent = BusinessAgent(EmptyStore())

    first = agent.answer("employee-1", "客户投诉产品质量")
    assert "客户希望如何处理" in first.content
    second = agent.answer("employee-1", "0689产品，产品质量出现大量问题")
    assert "客户希望如何处理" in second.content
    final = agent.answer("employee-1", "向上申请，领导同意，就可以退货")

    assert final.scenario == ReplyScenario.AFTER_SALES_COMPLAINT
    assert "直接回复版" in final.content
    assert "employee-1" not in agent._reply_sessions


def test_reply_session_fields_are_persisted_with_redis(monkeypatch):
    FakeRedis.data.clear()
    monkeypatch.setattr(
        "app.business.session.redis.Redis.from_url",
        lambda *_args, **_kwargs: FakeRedis(),
    )
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(
            llm_api_key=None,
            llm_model=None,
            redis_url="redis://fake",
            embedding_api_key=None,
            embedding_model=None,
            task_planner_enabled=False,
            intent_confidence_threshold=0.45,
        ),
    )
    agent = BusinessAgent(EmptyStore())
    agent.answer("employee-redis", "客户投诉产品质量")
    saved = json.loads(FakeRedis.data["wecom:reply-session:employee-redis"])
    assert saved["collected"]["issue"] == "客户投诉产品质量"

    follow_up = agent.answer("employee-redis", "0689产品，产品质量出现大量问题")
    assert "客户希望如何处理" in follow_up.content
    saved = json.loads(FakeRedis.data["wecom:reply-session:employee-redis"])
    assert saved["collected"]["product"] == "0689"


def test_semantic_router_can_leave_unfinished_reply_task(monkeypatch):
    monkeypatch.setattr(
        "app.business.service.get_settings",
        lambda: SimpleNamespace(llm_api_key=None, llm_model=None),
    )
    decisions = iter(
        [
            IntentDecision(
                Intent.REPLY_SCRIPT,
                0.98,
                "员工需要处理客户议价",
                "客户觉得价格太高",
                source="model",
                reply_scenario=ReplyScenario.PRICE_NEGOTIATION,
            ),
            IntentDecision(
                Intent.POLICY,
                0.96,
                "员工已切换到考勤制度查询",
                "公司迟到规定",
                source="model",
            ),
        ]
    )
    monkeypatch.setattr(
        "app.business.service.decide_intent", lambda *_args, **_kwargs: next(decisions)
    )
    monkeypatch.setattr(
        "app.business.service.get_rag_agent",
        lambda: SimpleNamespace(
            answer=lambda *_args, **_kwargs: SimpleNamespace(content="制度回答", sources=[])
        ),
    )
    agent = BusinessAgent(EmptyStore())

    agent.answer("employee-1", "客户觉得价格太高")
    reply = agent.answer("employee-1", "公司迟到是怎么规定的？")

    assert reply.intent == Intent.POLICY
    assert reply.content == "制度回答"
    assert "employee-1" not in agent._reply_sessions
