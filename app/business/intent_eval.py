import json
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from app.business.intent import Intent, IntentDecision, MediaAction, ReplyScenario

DATA_SOURCE_BY_INTENT: dict[Intent, str] = {
    Intent.POLICY: "knowledge_documents",
    Intent.REPLY_SCRIPT: "reply_scripts",
    Intent.PRODUCT: "product_catalog",
    Intent.PRODUCT_MEDIA: "product_media",
    Intent.GENERAL: "knowledge_documents",
    Intent.CONVERSATION: "none",
}


@dataclass(frozen=True)
class IntentEvalCase:
    case_id: str
    turns: tuple[dict[str, str], ...]
    expected_intent: Intent
    expected_data_source: str
    should_clarify: bool = False
    expected_query_keywords: tuple[str, ...] = ()
    expected_media_action: MediaAction | None = None
    expected_media_type: str | None = None
    expected_media_role: str | None = None
    expected_reply_scenario: ReplyScenario | None = None
    tags: tuple[str, ...] = ()
    notes: str = ""

    @property
    def current_message(self) -> str:
        return self.turns[-1]["content"]

    @property
    def recent_context(self) -> list[dict[str, str]]:
        return [dict(turn) for turn in self.turns[:-1]]

    @classmethod
    def from_dict(cls, value: dict[str, object], *, line_number: int) -> "IntentEvalCase":
        location = f"第 {line_number} 行"
        case_id = str(value.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"{location}缺少 case_id")
        raw_turns = value.get("turns")
        if not isinstance(raw_turns, list) or not raw_turns:
            raise ValueError(f"{case_id} 的 turns 必须是非空数组")
        turns: list[dict[str, str]] = []
        for turn in raw_turns:
            if not isinstance(turn, dict):
                raise ValueError(f"{case_id} 包含无效对话轮次")
            role = str(turn.get("role") or "").strip()
            content = str(turn.get("content") or "").strip()
            if role not in {"user", "assistant", "system"} or not content:
                raise ValueError(f"{case_id} 包含缺少角色或正文的对话轮次")
            normalized = {"role": role, "content": content}
            if turn.get("intent"):
                normalized["intent"] = str(turn["intent"])
            turns.append(normalized)
        if turns[-1]["role"] != "user":
            raise ValueError(f"{case_id} 的最后一轮必须是 user")
        try:
            expected_intent = Intent(str(value["expected_intent"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{case_id} 的 expected_intent 无效") from exc
        expected_data_source = str(value.get("expected_data_source") or "").strip()
        if not expected_data_source:
            raise ValueError(f"{case_id} 缺少 expected_data_source")
        expected_source = DATA_SOURCE_BY_INTENT[expected_intent]
        if expected_data_source != expected_source:
            raise ValueError(
                f"{case_id} 的数据源应为 {expected_source}，实际标注为 {expected_data_source}"
            )
        keywords = value.get("expected_query_keywords") or []
        tags = value.get("tags") or []
        if not isinstance(keywords, list) or not isinstance(tags, list):
            raise ValueError(f"{case_id} 的关键词和标签必须是数组")
        return cls(
            case_id=case_id,
            turns=tuple(turns),
            expected_intent=expected_intent,
            expected_data_source=expected_data_source,
            should_clarify=bool(value.get("should_clarify", False)),
            expected_query_keywords=tuple(str(item) for item in keywords),
            expected_media_action=_optional_enum(MediaAction, value.get("expected_media_action")),
            expected_media_type=_optional_text(value.get("expected_media_type")),
            expected_media_role=_optional_text(value.get("expected_media_role")),
            expected_reply_scenario=_optional_enum(
                ReplyScenario, value.get("expected_reply_scenario")
            ),
            tags=tuple(str(item) for item in tags),
            notes=str(value.get("notes") or ""),
        )


def _optional_enum(enum_type: type[MediaAction] | type[ReplyScenario], value: object):
    if value in (None, ""):
        return None
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise ValueError(f"无效枚举值：{value}") from exc


def _optional_text(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def load_eval_cases(path: Path) -> list[IntentEvalCase]:
    cases: list[IntentEvalCase] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
            case = IntentEvalCase.from_dict(value, line_number=line_number)
            if case.case_id in seen_ids:
                raise ValueError(f"case_id 重复：{case.case_id}")
            seen_ids.add(case.case_id)
            cases.append(case)
    if not cases:
        raise ValueError("评测集为空")
    return cases


def _case_result(
    case: IntentEvalCase, decision: IntentDecision, latency_ms: float
) -> dict[str, object]:
    actual_source = DATA_SOURCE_BY_INTENT[decision.intent]
    normalized_query = decision.query_for_search.lower()
    missing_keywords = [
        keyword
        for keyword in case.expected_query_keywords
        if keyword.lower() not in normalized_query
    ]
    checks = {
        "intent": decision.intent == case.expected_intent,
        "data_source": actual_source == case.expected_data_source,
        "clarification": decision.needs_clarification == case.should_clarify,
        "query_keywords": not missing_keywords,
        "media_action": (
            case.expected_media_action is None
            or decision.media_action == case.expected_media_action
        ),
        "media_type": (
            case.expected_media_type is None or decision.media_type == case.expected_media_type
        ),
        "media_role": (
            case.expected_media_role is None or decision.media_role == case.expected_media_role
        ),
        "reply_scenario": (
            case.expected_reply_scenario is None
            or decision.reply_scenario == case.expected_reply_scenario
        ),
    }
    return {
        "case_id": case.case_id,
        "tags": list(case.tags),
        "message": case.current_message,
        "expected": {
            "intent": str(case.expected_intent),
            "data_source": case.expected_data_source,
            "should_clarify": case.should_clarify,
            "query_keywords": list(case.expected_query_keywords),
            "media_action": str(case.expected_media_action) if case.expected_media_action else None,
            "media_type": case.expected_media_type,
            "media_role": case.expected_media_role,
            "reply_scenario": (
                str(case.expected_reply_scenario) if case.expected_reply_scenario else None
            ),
        },
        "actual": {
            "intent": str(decision.intent),
            "data_source": actual_source,
            "needs_clarification": decision.needs_clarification,
            "query_for_search": decision.query_for_search,
            "missing_query_keywords": missing_keywords,
            "media_action": str(decision.media_action) if decision.media_action else None,
            "media_type": decision.media_type,
            "media_role": decision.media_role,
            "reply_scenario": str(decision.reply_scenario) if decision.reply_scenario else None,
            "confidence": decision.confidence,
            "source": decision.source,
            "reason": decision.reason,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "latency_ms": round(latency_ms, 2),
        "notes": case.notes,
    }


def run_intent_evaluation(
    cases: list[IntentEvalCase],
    decision_fn: Callable[[IntentEvalCase], IntentDecision],
    *,
    workers: int = 1,
    mode: str = "model",
) -> dict[str, object]:
    """运行相互隔离的意图评测，不创建或读取任何员工会话状态。"""

    def run_one(index: int, case: IntentEvalCase) -> tuple[int, dict[str, object]]:
        started = time.perf_counter()
        decision = decision_fn(case)
        latency_ms = (time.perf_counter() - started) * 1000
        return index, _case_result(case, decision, latency_ms)

    ordered: list[dict[str, object] | None] = [None] * len(cases)
    if workers <= 1:
        for index, case in enumerate(cases):
            _, result = run_one(index, case)
            ordered[index] = result
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="intent-eval") as pool:
            futures = {pool.submit(run_one, index, case): index for index, case in enumerate(cases)}
            for future in as_completed(futures):
                index, result = future.result()
                ordered[index] = result
    results = [result for result in ordered if result is not None]
    total = len(results)
    confusion: dict[str, dict[str, int]] = {
        str(intent): {str(actual): 0 for actual in Intent} for intent in Intent
    }
    for result in results:
        expected = str(result["expected"]["intent"])
        actual = str(result["actual"]["intent"])
        confusion[expected][actual] += 1
    latencies = sorted(float(result["latency_ms"]) for result in results)
    context_results = [result for result in results if "context" in result["tags"]]

    def rate(predicate: Callable[[dict[str, object]], bool], subset=results) -> float:
        return (
            round(sum(1 for item in subset if predicate(item)) / len(subset), 4) if subset else 0.0
        )

    summary = {
        "total_cases": total,
        "passed_cases": sum(bool(result["passed"]) for result in results),
        "overall_pass_rate": rate(lambda item: bool(item["passed"])),
        "intent_accuracy": rate(lambda item: bool(item["checks"]["intent"])),
        "context_accuracy": rate(lambda item: bool(item["passed"]), context_results),
        "clarification_accuracy": rate(lambda item: bool(item["checks"]["clarification"])),
        "query_keyword_accuracy": rate(lambda item: bool(item["checks"]["query_keywords"])),
        "wrong_data_source_rate": rate(lambda item: not bool(item["checks"]["data_source"])),
        "fallback_rate": rate(lambda item: item["actual"]["source"] == "rules"),
        "fast_path_rate": rate(lambda item: item["actual"]["source"] == "deterministic_fast_path"),
        "model_route_rate": rate(lambda item: item["actual"]["source"] == "model"),
        "average_latency_ms": round(sum(latencies) / total, 2),
        "p95_latency_ms": latencies[max(0, math.ceil(total * 0.95) - 1)],
    }
    return {
        "schema_version": 1,
        "mode": mode,
        "summary": summary,
        "confusion_matrix": confusion,
        "failures": [result for result in results if not result["passed"]],
        "results": results,
        "limitations": [
            "本报告只评测意图决策层，不评测检索召回率和最终回答事实性。",
            "无依据编造率需要在下一阶段使用固定资料快照评测完整回答链路。",
        ],
    }
