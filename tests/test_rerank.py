from app.rerank import RerankScore, _parse_results, rerank_matches
from app.retrieval import HybridMatch


def test_parse_dashscope_results_uses_document_indexes() -> None:
    scores = _parse_results(
        {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.82},
                    {"index": 0, "relevance_score": 0.41},
                ]
            }
        },
        ["first", "second"],
    )
    assert scores == [RerankScore("second", 0.82), RerankScore("first", 0.41)]


def test_rerank_matches_falls_back_when_provider_returns_no_scores(monkeypatch) -> None:
    monkeypatch.setattr("app.rerank.dashscope_rerank", lambda query, documents: [])
    matches = [
        HybridMatch("a", 0.9, 0.8, 0.7),
        HybridMatch("b", 0.8, 0.7, 0.6),
    ]
    result = rerank_matches("问题", {"a": "甲", "b": "乙"}, matches, limit=1)
    assert [item.key for item in result] == ["a"]


def test_rerank_matches_uses_external_order(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.rerank.dashscope_rerank",
        lambda query, documents: [RerankScore("b", 0.95), RerankScore("a", 0.1)],
    )
    matches = [
        HybridMatch("a", 0.9, 0.8, 0.7),
        HybridMatch("b", 0.8, 0.7, 0.6),
    ]
    result = rerank_matches("问题", {"a": "甲", "b": "乙"}, matches, limit=2)
    assert [item.key for item in result] == ["b", "a"]
