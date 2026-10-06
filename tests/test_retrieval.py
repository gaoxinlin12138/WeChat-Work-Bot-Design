from app.retrieval import bm25_scores, hybrid_rerank, tokenize


def test_chinese_tokenizer_keeps_codes_and_uses_bigrams() -> None:
    terms = tokenize("请查一下 0689 的考勤规定")

    assert "0689" in terms
    assert "考勤" in terms
    assert "规定" in terms


def test_bm25_prefers_document_with_matching_policy_terms() -> None:
    scores = bm25_scores(
        "员工迟到如何处罚",
        {
            "attendance": "考勤管理制度 员工迟到三次按照规定处理",
            "shipping": "产品发货时间和物流查询说明",
        },
    )

    assert scores["attendance"] == 1.0
    assert "shipping" not in scores


def test_hybrid_rerank_unites_semantic_and_lexical_candidates() -> None:
    matches = hybrid_rerank(
        "LTA-822是什么材质几档",
        {
            "semantic": "置物花洒包含顶喷和手喷",
            "lexical": "产品型号LTA-822 材质：全铝 档位：4",
            "irrelevant": "员工考勤管理制度",
        },
        {"semantic": 0.92, "irrelevant": 0.12},
        limit=2,
    )

    assert [match.key for match in matches] == ["lexical", "semantic"]
    assert matches[0].bm25_score == 1.0


def test_quality_score_breaks_similar_script_tie() -> None:
    matches = hybrid_rerank(
        "客户觉得价格高",
        {"low": "客户觉得价格高", "high": "客户觉得价格高"},
        {},
        limit=2,
        quality_scores={"low": 0.2, "high": 0.9},
    )

    assert [match.key for match in matches] == ["high", "low"]
