from collections import Counter
from pathlib import Path

from app.business.intent import Intent, IntentDecision
from app.business.intent_eval import load_eval_cases, run_intent_evaluation

CASES_PATH = Path(__file__).resolve().parents[1] / "evals" / "intent_cases.jsonl"


def test_intent_eval_dataset_has_balanced_coverage() -> None:
    cases = load_eval_cases(CASES_PATH)
    counts = Counter(case.expected_intent for case in cases)

    assert len(cases) == 75
    assert all(count >= 10 for count in counts.values())
    assert len(counts) == 6
    assert sum("context" in case.tags for case in cases) >= 10
    assert sum("typo" in case.tags for case in cases) >= 2
    assert sum("adversarial" in case.tags for case in cases) >= 2
    assert sum("clarify" in case.tags for case in cases) >= 2


def test_intent_evaluation_scores_all_expected_fields() -> None:
    cases = load_eval_cases(CASES_PATH)[:5]

    def exact_decision(case):
        return IntentDecision(
            intent=case.expected_intent,
            confidence=0.99,
            reason="测试决策",
            query_for_search=" ".join(case.expected_query_keywords) or case.current_message,
            source="model",
            needs_clarification=case.should_clarify,
            media_action=case.expected_media_action,
            media_type=case.expected_media_type,
            media_role=case.expected_media_role,
            reply_scenario=case.expected_reply_scenario,
        )

    report = run_intent_evaluation(cases, exact_decision, workers=2)

    assert report["summary"]["overall_pass_rate"] == 1.0
    assert report["summary"]["intent_accuracy"] == 1.0
    assert report["summary"]["wrong_data_source_rate"] == 0.0
    assert report["failures"] == []


def test_intent_evaluation_exposes_wrong_route() -> None:
    case = load_eval_cases(CASES_PATH)[0]

    report = run_intent_evaluation(
        [case],
        lambda _case: IntentDecision(
            intent=Intent.CONVERSATION,
            confidence=0.8,
            reason="故意错误",
            query_for_search=_case.current_message,
            source="model",
        ),
    )

    assert report["summary"]["intent_accuracy"] == 0.0
    assert report["summary"]["wrong_data_source_rate"] == 1.0
    assert report["failures"][0]["checks"]["intent"] is False
