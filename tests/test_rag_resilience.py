from types import SimpleNamespace

import pytest

from app.agent import RagAgent
from app.config import Settings
from app.knowledge.store import ChatMessage, SearchResult


class EvidenceStore:
    def __init__(self):
        self.saved = []
        self.searched = []

    def search(self, query, limit, query_embedding=None):
        self.searched.append((query, query_embedding))
        return [SearchResult("policy", "请假制度", "制度", "2.1 年假提前三天申请。", 5, 0.9)]

    def add_message(self, user_id, role, content):
        self.saved.append((role, content))

    def recent_messages(self, user_id, limit):
        return [ChatMessage("user", "上一轮问题")]


def settings(**values):
    return Settings(
        _env_file=None,
        llm_api_key="fixture",
        llm_model="fixture",
        embedding_api_key="fixture",
        embedding_model="fixture",
        **values,
    )


def fail(*args, **kwargs):
    raise TimeoutError("模拟服务超时")


def test_embedding_timeout_keeps_keyword_evidence(monkeypatch):
    store = EvidenceStore()
    agent = RagAgent(settings(), store)
    monkeypatch.setattr(agent, "embed", fail)
    results = agent.search("年假")
    assert results[0].title == "请假制度"
    assert store.searched == [("年假", None)]


def test_llm_timeout_returns_source_text_and_actual_citation(monkeypatch):
    agent = RagAgent(settings(), EvidenceStore())
    monkeypatch.setattr(agent, "_llm_client", fail)
    reply = agent.answer("isolated", "年假", query_embedding=[0.1])
    assert "归纳服务暂时不可用" in reply.content
    assert "年假提前三天申请" in reply.content
    assert "《请假制度》｜第2.1条｜PDF第5页" in reply.content
    assert "[E" not in reply.content


@pytest.mark.parametrize("history_limit", [0, 6])
def test_current_question_is_always_sent_even_if_history_is_disabled(monkeypatch, history_limit):
    store = EvidenceStore()
    agent = RagAgent(settings(rag_history_messages=history_limit), store)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["messages"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="应提前三天申请。[E1:2.1]"))]
        )

    monkeypatch.setattr(
        agent,
        "_llm_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete))),
    )
    agent.answer("isolated", "年假需要提前几天？", query_embedding=[0.1])
    assert calls[0][-1] == {"role": "user", "content": "年假需要提前几天？"}
    assert sum(m["content"] == "年假需要提前几天？" for m in calls[0]) == 1
    assert len(calls[0]) == (2 if history_limit == 0 else 3)


def test_history_write_failure_does_not_block_answer(monkeypatch):
    store = EvidenceStore()
    agent = RagAgent(settings(), store)
    monkeypatch.setattr(store, "add_message", fail)
    monkeypatch.setattr(agent, "_llm_client", fail)
    reply = agent.answer("isolated", "年假", query_embedding=[0.1])
    assert "年假提前三天申请" in reply.content
