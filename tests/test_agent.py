from types import SimpleNamespace

import pytest

from app.agent import RagAgent, append_consolidated_citations, clean_streaming_answer
from app.config import Settings
from app.knowledge.store import SearchResult


class FakeEmbeddings:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        self.calls.append(input)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index)] * self.dimensions)
                for index in range(len(input))
            ]
        )


class FakeClient:
    def __init__(self, embeddings: FakeEmbeddings) -> None:
        self.embeddings = embeddings


class FakeStore:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.messages: list[tuple[str, str, str]] = []

    def search(self, query: str, limit: int, query_embedding: list[float] | None):
        return self.results[:limit]

    def add_message(self, user_id: str, role: str, content: str) -> None:
        self.messages.append((user_id, role, content))


def test_embedding_batches_and_validates_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        embedding_api_key="key",
        embedding_model="embedding-test",
        embedding_dimensions=3,
        embedding_batch_size=2,
    )
    fake_embeddings = FakeEmbeddings(3)
    agent = RagAgent(settings, object())
    monkeypatch.setattr(agent, "_embedding_client", lambda: FakeClient(fake_embeddings))

    vectors = agent.embed(["a", "b", "c"])

    assert len(vectors) == 3
    assert fake_embeddings.calls == [["a", "b"], ["c"]]


def test_embedding_caps_provider_batch_at_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        embedding_api_key="key",
        embedding_model="text-embedding-v3",
        embedding_dimensions=3,
        embedding_batch_size=32,
    )
    fake_embeddings = FakeEmbeddings(3)
    agent = RagAgent(settings, object())
    monkeypatch.setattr(agent, "_embedding_client", lambda: FakeClient(fake_embeddings))

    agent.embed([str(index) for index in range(12)])

    assert [len(batch) for batch in fake_embeddings.calls] == [10, 2]


def test_embedding_rejects_wrong_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        embedding_api_key="key",
        embedding_model="embedding-test",
        embedding_dimensions=3,
    )
    fake_embeddings = FakeEmbeddings(2)
    agent = RagAgent(settings, object())
    monkeypatch.setattr(agent, "_embedding_client", lambda: FakeClient(fake_embeddings))

    with pytest.raises(RuntimeError, match="维度"):
        agent.embed(["a"])


def test_semantic_search_filters_results_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        embedding_api_key="key",
        embedding_model="embedding-test",
        embedding_dimensions=3,
        rag_min_score=0.35,
    )
    results = [
        SearchResult("low", "无关资料", "测试", "无关内容", None, 0.27),
        SearchResult("high", "相关资料", "测试", "相关内容", None, 0.43),
    ]
    agent = RagAgent(settings, FakeStore(results))
    monkeypatch.setattr(agent, "embed", lambda texts: [[0.0, 0.0, 0.0]])

    assert [result.document_id for result in agent.search("问题")] == ["high"]


def test_answer_refuses_without_calling_llm_when_no_reliable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        embedding_api_key="key",
        embedding_model="embedding-test",
        embedding_dimensions=3,
        llm_api_key="key",
        llm_model="chat-test",
    )
    store = FakeStore([])
    agent = RagAgent(settings, store)
    monkeypatch.setattr(agent, "embed", lambda texts: [[0.0, 0.0, 0.0]])
    monkeypatch.setattr(
        agent,
        "_llm_client",
        lambda: pytest.fail("没有可靠来源时不应调用 LLM"),
    )

    reply = agent.answer("user", "公司的报销流程是什么？")

    assert reply.sources == []
    assert reply.content == "知识库中没有足够依据，暂时无法可靠回答。"
    assert "资料" not in reply.content


def test_answer_citations_are_consolidated_at_the_end() -> None:
    results = [
        SearchResult("id", "考勤制度", "制度", "6.5.1 应提前申请。", 5, 0.8, 1),
        SearchResult("id", "考勤制度", "制度", "6.5.2 应在次月使用。", 5, 0.7, 2),
    ]
    content = (
        "一、申请\n应提前申请。【《考勤制度》｜第6.5.1条｜PDF第5页】\n\n"
        "引用依据：\n- 模型自行生成的内容"
    )
    formatted = append_consolidated_citations(content, results)

    assert formatted.startswith("一、申请\n应提前申请。")
    assert formatted.count("引用依据：") == 1
    assert "模型自行生成的内容" not in formatted
    assert formatted.endswith("【《考勤制度》｜第6.5.1、6.5.2条｜PDF第5页】")


def test_answer_uses_only_model_selected_and_valid_clauses() -> None:
    results = [
        SearchResult("id", "考勤制度", "制度", "6.5.1 应提前申请。6.5.2 次周使用。", 5, 0.8, 1),
        SearchResult("id", "考勤制度", "制度", "3.6 调休定义。", 3, 0.7, 2),
    ]
    formatted = append_consolidated_citations(
        "调休应提前申请。[E1:6.5.1、9.9] 调休定义如下。[E2:3.6]",
        results,
    )

    assert "[E" not in formatted
    assert "9.9" not in formatted
    assert "【《考勤制度》｜第6.5.1条｜PDF第5页】" in formatted
    assert formatted.endswith("【《考勤制度》｜第3.6条｜PDF第3页】")


def test_streaming_answer_never_exposes_internal_marker() -> None:
    assert clean_streaming_answer("员工应提前申请。[E1:6.5") == "员工应提前申请。"
    assert clean_streaming_answer("员工应提前申请。[E1:6.5.1]") == "员工应提前申请。"


def test_generate_answer_uses_provider_stream_and_emits_clean_text(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        llm_api_key="key",
        llm_model="chat-test",
    )
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="应提前申请。"))]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="[E1:6.5"))]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=".1]"))]
                    ),
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent = RagAgent(settings, object())
    monkeypatch.setattr(agent, "_llm_client", lambda: client)
    updates = []

    content = agent._generate_answer("怎么请假", "资料", [], updates.append)

    assert captured["stream"] is True
    assert content == "应提前申请。[E1:6.5.1]"
    assert updates[-1] == "应提前申请。"
    assert all("[E" not in update for update in updates)
