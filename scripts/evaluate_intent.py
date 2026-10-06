import argparse
import json
from pathlib import Path

from openai import OpenAI

from app.business.intent import decide_intent
from app.business.intent_eval import IntentEvalCase, load_eval_cases, run_intent_evaluation
from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "evals" / "intent_cases.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "intent_eval_report.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线评测企业微信机器人的意图识别")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mode", choices=("model", "rules"), default="model")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fail-under", type=float, default=0.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_eval_cases(args.cases)
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit 必须大于 0")
        cases = cases[: args.limit]
    settings = get_settings()
    if args.mode == "model":
        if not (settings.llm_api_key and settings.llm_model and settings.llm_base_url):
            raise SystemExit("模型评测需要配置 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL")
        # 模型评测只访问模型 API，不初始化生产知识库或 Redis。
        client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
        )

        def decision_fn(case: IntentEvalCase):
            return decide_intent(
                case.current_message,
                recent_context=case.recent_context,
                client=client,
                model=settings.llm_model,
                confidence_threshold=settings.intent_confidence_threshold,
            )

    else:

        def decision_fn(case: IntentEvalCase):
            # Exercise the same high-precision fast paths and fallback as production
            # with the model deliberately unavailable.
            return decide_intent(case.current_message, recent_context=case.recent_context)

    report = run_intent_evaluation(
        cases,
        decision_fn,
        workers=max(1, args.workers),
        mode=args.mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(f"评测完成：{summary['passed_cases']}/{summary['total_cases']} 项全部检查通过")
    print(f"主意图准确率：{summary['intent_accuracy']:.2%}")
    print(f"上下文用例通过率：{summary['context_accuracy']:.2%}")
    print(f"错误数据源比例：{summary['wrong_data_source_rate']:.2%}")
    print(f"平均/P95耗时：{summary['average_latency_ms']:.0f}/{summary['p95_latency_ms']:.0f} ms")
    print(f"失败用例：{len(report['failures'])}，完整报告：{args.output}")
    if summary["intent_accuracy"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
