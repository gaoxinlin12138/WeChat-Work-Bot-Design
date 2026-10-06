from app.business.chain_eval import (
    ChainEvalCase,
    ChainReply,
    evaluate_chain_case,
    run_chain_evaluation,
)
from app.business.intent import Intent


def test_chain_eval_requires_evidence_and_citation() -> None:
    case = ChainEvalCase(
        "policy", "迟到规定", Intent.POLICY, "document", ("考勤",), ("迟到",), True
    )
    reply = ChainReply(
        Intent.POLICY,
        "迟到按考勤制度执行。依据【《员工考勤制度》｜第1条｜PDF第3页】",
        [
            {
                "source_type": "document",
                "title": "员工考勤制度",
                "content": "1. 迟到",
                "citation": "【《员工考勤制度》｜第1条｜PDF第3页】",
            }
        ],
    )
    assert evaluate_chain_case(case, reply, 1)["passed"] is True


def test_chain_eval_flags_unsupported_answer() -> None:
    case = ChainEvalCase(
        "missing", "9999", Intent.PRODUCT, "none", (), ("没有找到",), expect_no_evidence=True
    )
    reply = ChainReply(
        Intent.PRODUCT, "9999价格是100元", [{"source_type": "product", "title": "其他产品"}]
    )
    result = evaluate_chain_case(case, reply, 1)
    assert result["passed"] is False
    assert result["checks"]["no_evidence"] is False


def test_chain_evaluation_report_contains_failures() -> None:
    case = ChainEvalCase("one", "问候", Intent.CONVERSATION, "none")
    report = run_chain_evaluation([case], lambda _case: ChainReply(Intent.PRODUCT, "答复", []))
    assert report["summary"]["intent_accuracy"] == 0.0
    assert len(report["failures"]) == 1


def test_fixture_runner_keeps_live_dependencies_untouched(monkeypatch) -> None:
    import app.business.service as service
    from scripts.evaluate_chain import CASES, build_runner, load_cases

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("评测不应连接线上数据库或 Redis")

    original_settings = service.get_settings
    original_agent = service.get_rag_agent
    monkeypatch.setattr("psycopg.connect", unexpected_connection)
    monkeypatch.setattr("redis.Redis.from_url", unexpected_connection)
    report = run_chain_evaluation(load_cases(CASES), build_runner(False))
    assert report["failures"] == []
    assert service.get_settings is original_settings
    assert service.get_rag_agent is original_agent
