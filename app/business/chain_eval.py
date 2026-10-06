import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.business.intent import Intent


@dataclass(frozen=True)
class ChainEvalCase:
    case_id: str
    query: str
    expected_intent: Intent
    expected_data_source: str
    expected_source_keywords: tuple[str, ...] = ()
    expected_answer_keywords: tuple[str, ...] = ()
    citation_required: bool = False
    expect_no_evidence: bool = False
    expect_clarification: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChainReply:
    intent: Intent
    content: str
    sources: list[dict[str, object]]
    needs_clarification: bool = False


def _source_text(sources: list[dict[str, object]]) -> str:
    return "\n".join(
        " ".join(str(item.get(key) or "") for key in ("title", "content", "citation"))
        for item in sources
    ).lower()


def evaluate_chain_case(
    case: ChainEvalCase, reply: ChainReply, latency_ms: float
) -> dict[str, object]:
    source_text = _source_text(reply.sources)
    answer = reply.content or ""
    answer_lower = answer.lower()
    missing_sources = [
        keyword for keyword in case.expected_source_keywords if keyword.lower() not in source_text
    ]
    missing_answer = [
        keyword for keyword in case.expected_answer_keywords if keyword.lower() not in answer_lower
    ]
    if case.expect_no_evidence:
        no_evidence_ok = not reply.sources and any(
            marker in answer
            for marker in (
                "没有足够依据",
                "没有找到足够依据",
                "没有找到对应产品",
                "暂未找到",
                "无法可靠回答",
                "还没有登记",
            )
        )
    else:
        no_evidence_ok = True
    if case.citation_required:
        citation_ok = bool(reply.sources) and "【《" in answer and "PDF第" in answer
    else:
        citation_ok = True
    checks = {
        "intent": reply.intent == case.expected_intent,
        "data_source": case.expected_data_source
        in {str(source.get("source_type") or "") for source in reply.sources}
        or (case.expected_data_source == "none" and not reply.sources),
        "source_keywords": not missing_sources,
        "answer_keywords": not missing_answer,
        "citation": citation_ok,
        "no_evidence": no_evidence_ok,
        "clarification": reply.needs_clarification == case.expect_clarification,
    }
    return {
        "case_id": case.case_id,
        "query": case.query,
        "tags": list(case.tags),
        "expected": {
            "intent": str(case.expected_intent),
            "data_source": case.expected_data_source,
            "source_keywords": list(case.expected_source_keywords),
            "answer_keywords": list(case.expected_answer_keywords),
            "citation_required": case.citation_required,
            "expect_no_evidence": case.expect_no_evidence,
            "expect_clarification": case.expect_clarification,
        },
        "actual": {
            "intent": str(reply.intent),
            "content": answer,
            "sources": reply.sources,
            "needs_clarification": reply.needs_clarification,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "latency_ms": round(latency_ms, 2),
        "missing_source_keywords": missing_sources,
        "missing_answer_keywords": missing_answer,
    }


def run_chain_evaluation(
    cases: list[ChainEvalCase],
    reply_fn: Callable[[ChainEvalCase], ChainReply],
    *,
    mode: str = "fixture",
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for case in cases:
        started = time.perf_counter()
        reply = reply_fn(case)
        results.append(evaluate_chain_case(case, reply, (time.perf_counter() - started) * 1000))
    total = len(results)
    latencies = sorted(float(item["latency_ms"]) for item in results)

    def rate(predicate: Callable[[dict[str, object]], bool], subset=results) -> float:
        return (
            round(sum(1 for item in subset if predicate(item)) / len(subset), 4) if subset else 0.0
        )

    policy = [item for item in results if item["expected"]["intent"] == str(Intent.POLICY)]
    return {
        "schema_version": 1,
        "mode": mode,
        "summary": {
            "total_cases": total,
            "passed_cases": sum(bool(item["passed"]) for item in results),
            "overall_pass_rate": rate(lambda item: bool(item["passed"])),
            "intent_accuracy": rate(lambda item: bool(item["checks"]["intent"])),
            "source_hit_rate": rate(lambda item: bool(item["checks"]["source_keywords"])),
            "answer_keyword_rate": rate(lambda item: bool(item["checks"]["answer_keywords"])),
            "citation_accuracy": rate(lambda item: bool(item["checks"]["citation"]), policy),
            "no_evidence_honesty_rate": rate(lambda item: bool(item["checks"]["no_evidence"])),
            "clarification_accuracy": rate(lambda item: bool(item["checks"]["clarification"])),
            "average_latency_ms": round(sum(latencies) / total, 2) if total else 0.0,
            "p95_latency_ms": latencies[max(0, math.ceil(total * 0.95) - 1)] if total else 0.0,
        },
        "failures": [item for item in results if not item["passed"]],
        "results": results,
        "limitations": [
            "fixture 模式使用固定脱敏资料快照，验证路由、检索接线和回答约束，不写入生产数据库。",
            "最终上线前仍需在隔离环境用真实已发布资料抽样复测。",
        ],
    }
