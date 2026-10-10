"""用标注语料测量理解层的计划匹配率。

这是「不管怎么问都能正确识别意图」的可执行定义：对每条已标注的真实提问，
比较当前规划器给出的计划与期望计划。

指标口径：

- **计划匹配率**：domain、op、是否澄清三项全对。
- **该澄清时澄清率**：期望澄清的用例中确实澄清的比例（漏澄清会答错，最伤）。
- **不乱澄清率**：期望直接执行的用例中没有多问的比例（乱澄清会烦人）。
- 带 `policy-pending` 标签的用例（`实物图`/`主图` 这类旧叫法是否改收）是
  待业务决策的，不计入上述指标，单独列出。

因为是离线规则路径（见 offline_env），这里测的是**降级路径**的理解能力；
线上由模型决策时的表现需要 `--mode model` 另行测量。
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from app.business.intent import Intent
from scripts.offline_env import OfflineBusiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "plan_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "plan_eval_report.json"

DOMAIN_BY_INTENT = {
    Intent.POLICY: "policy",
    Intent.GENERAL: "general",
    Intent.PRODUCT: "product",
    Intent.PRODUCT_MEDIA: "media",
    Intent.REPLY_SCRIPT: "script",
    Intent.CONVERSATION: "conversation",
}
PENDING_TAG = "policy-pending"
# 「换主体时不得继承」这类负向用例必须用真实资料验证：离线 fixture 的
# search_products 会忽略查询、对任何型号都返回同一个产品，所以在离线环境里
# 它们只会测出 fixture 的行为，不是被测代码的行为。
REAL_DATA_TAG = "needs-real-data"
EXCLUDED_TAGS = (PENDING_TAG, REAL_DATA_TAG)
# 计划形状正确但执行没接上时的拒答话术。只看计划会漏掉这类失败：
# 例如多轮里「换成主体图」的计划是 media、形状完全正确，但缺产品型号，
# 员工实际收到「请先提供具体产品型号」。
REFUSAL_MARKERS = (
    "请先提供",
    "没有找到",
    "没有已发布",
    "没有足够依据",
    "暂时无法",
    "暂时不可用",
)


def actual_plan_fields(plan) -> tuple[str | None, bool]:
    """从 TaskPlan 提取 (op, 是否需要澄清)。"""
    product_task = next((task for task in plan.tasks if task.product_query is not None), None)
    op = product_task.product_query.operation if product_task else None
    return op, bool(plan.needs_clarification)


def rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="测量理解层的计划匹配率")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in args.cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    business = OfflineBusiness()
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases, 1):
        turns = list(case.get("turns") or [case["query"]])
        # 多轮用例在同一会话内按顺序执行，只对**最后一轮**评分：前面几轮只是铺垫。
        session = business.invoke_turns(index, turns)
        plan, reply = session[-1]
        # 记录前面几轮是否提出过澄清：这决定了「澄清能不能被接住」这个指标的分母。
        clarified_earlier = any(item[0].needs_clarification for item in session[:-1])
        expected = case["expected"]
        actual_domain = DOMAIN_BY_INTENT.get(plan.primary_intent)
        actual_op, actual_clarify = actual_plan_fields(plan)
        expected_op = expected.get("op")
        answer_text = str(reply.content)
        # 负向断言：追问时必须继承上一轮主体，但**换主体时绝不能继承**。
        # 过度继承会把"LY0653GDT 的材质"答成上一轮的 LTA-822，是最危险的失败。
        forbidden = [item for item in expected.get("must_not_contain", []) if item in answer_text]
        # 跨域复合提问：一次要覆盖多个领域，只答一个域就是静默丢掉半个问题。
        actual_domains = {DOMAIN_BY_INTENT.get(task.decision.intent) for task in plan.tasks}
        missing_domains = [
            domain
            for domain in expected.get("must_include_domains", [])
            if domain not in actual_domains
        ]
        results.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "turns": turns,
                "is_multi_turn": len(turns) > 1,
                "clarified_earlier": clarified_earlier,
                "tags": case.get("tags", []),
                "expected": expected,
                "actual": {
                    "domain": actual_domain,
                    "op": actual_op,
                    "clarify": actual_clarify,
                    "scenario": (
                        str(plan.tasks[0].decision.reply_scenario)
                        if plan.tasks[0].decision.reply_scenario is not None
                        else None
                    ),
                    "plan_source": plan.source,
                    "goal": plan.goal,
                    "answer_preview": answer_text[:120],
                },
                "forbidden_hits": forbidden,
                "checks": {
                    "domain": actual_domain == expected["domain"],
                    "op": expected_op is None or actual_op == expected_op,
                    "clarify": actual_clarify == expected.get("must_clarify", False),
                    "no_over_inherit": not forbidden,
                    # 计划形状对但落到"请先提供型号/无法安全解析"这类死胡同，员工什么也拿不到。
                    # 只有显式声明 must_answer 的用例才检查这一项。
                    "answers": not expected.get("must_answer")
                    or not any(marker in answer_text for marker in REFUSAL_MARKERS),
                    # 话术场景是话术域的核心判断，只有声明了 scenario 的用例才检查。
                    "scenario": expected.get("scenario") is None
                    or (
                        plan.tasks[0].decision.reply_scenario is not None
                        and str(plan.tasks[0].decision.reply_scenario)
                        == expected["scenario"]
                    ),
                    "covers_all_domains": not missing_domains,
                },
                "missing_domains": missing_domains,
                "answer_is_refusal": any(
                    marker in answer_text for marker in REFUSAL_MARKERS
                ),
            }
        )

    for item in results:
        item["passed"] = all(item["checks"].values())

    scored = [
        item
        for item in results
        if not any(tag in item["tags"] for tag in EXCLUDED_TAGS)
    ]
    excluded = [item for item in results if any(tag in item["tags"] for tag in EXCLUDED_TAGS)]
    pending = [item for item in excluded if PENDING_TAG in item["tags"]]
    real_data = [item for item in excluded if REAL_DATA_TAG in item["tags"]]
    product_scored = [item for item in scored if item["expected"].get("op") is not None]
    should_clarify = [item for item in scored if item["expected"].get("must_clarify")]
    should_not_clarify = [item for item in scored if not item["expected"].get("must_clarify")]
    single_turn = [item for item in scored if not item["is_multi_turn"]]
    multi_turn = [item for item in scored if item["is_multi_turn"]]
    after_clarification = [item for item in scored if item["clarified_earlier"]]
    clarification_follow_up = sum(
        1 for item in after_clarification if not item["answer_is_refusal"]
    )
    scenario_cases = [item for item in scored if item["expected"].get("scenario")]

    summary = {
        "total": len(results),
        "scored": len(scored),
        "pending": len(pending),
        "needs_real_data": len(real_data),
        "passed": sum(1 for item in scored if item["passed"]),
        "plan_match_rate": rate(sum(1 for item in scored if item["passed"]), len(scored)),
        "domain_accuracy": rate(
            sum(1 for item in scored if item["checks"]["domain"]), len(scored)
        ),
        "op_accuracy": rate(
            sum(1 for item in product_scored if item["checks"]["op"]), len(product_scored)
        ),
        "clarify_recall": rate(
            sum(1 for item in should_clarify if item["checks"]["clarify"]),
            len(should_clarify),
        ),
        "no_over_clarify_rate": rate(
            sum(1 for item in should_not_clarify if item["checks"]["clarify"]),
            len(should_not_clarify),
        ),
        "single_turn_total": len(single_turn),
        "single_turn_match_rate": rate(
            sum(1 for item in single_turn if item["passed"]), len(single_turn)
        ),
        "scenario_total": len(scenario_cases),
        "scenario_accuracy": rate(
            sum(1 for item in scenario_cases if item["checks"]["scenario"]),
            len(scenario_cases),
        ),
        "multi_turn_total": len(multi_turn),
        "multi_turn_match_rate": rate(
            sum(1 for item in multi_turn if item["passed"]), len(multi_turn)
        ),
        "multi_turn_completed": sum(
            1 for item in multi_turn if not item["answer_is_refusal"]
        ),
        "multi_turn_completion_rate": rate(
            sum(1 for item in multi_turn if not item["answer_is_refusal"]),
            len(multi_turn),
        ),
        # 「问得出」不等于「接得住」：系统提出澄清后，员工照答能不能继续完成任务。
        "clarification_followed_up": clarification_follow_up,
        "clarification_follow_up_total": len(after_clarification),
        "clarification_follow_up_rate": rate(clarification_follow_up, len(after_clarification)),
        "plan_source_distribution": dict(
            Counter(item["actual"]["plan_source"] for item in results)
        ),
    }

    report = {
        "schema_version": 1,
        "cases": str(args.cases.relative_to(ROOT)),
        "mode": "rules-only-offline",
        "limitations": [
            "离线规则路径：只代表模型不可用时的降级能力，不代表线上模型决策表现。",
            "fixture 资料只有 1 个产品、4 份资料，因此只判定计划与路由，不判定检索质量。",
            "带 policy-pending 标签的用例待业务决策，未计入指标。",
        ],
        "summary": summary,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"计划评测：{len(scored)} 条计入指标（另 {len(pending)} 条待决策）")
    print()
    print("=== 核心指标（降级路径）===")
    print(f"  计划匹配率        {summary['plan_match_rate']:6.1%}")
    print(f"  域准确率          {summary['domain_accuracy']:6.1%}")
    print(
        f"  产品 op 准确率     {summary['op_accuracy']:6.1%}"
    )
    print(
        f"  话术场景准确率     {summary['scenario_accuracy']:6.1%}"
        f"  （{summary['scenario_total']} 条话术用例）"
    )
    print(f"  该澄清时澄清率     {summary['clarify_recall']:6.1%}")
    print(f"  不乱澄清率        {summary['no_over_clarify_rate']:6.1%}")
    print()
    print("=== 单轮 / 多轮分开看 ===")
    print(
        f"  单轮  {summary['single_turn_match_rate']:6.1%}"
        f"  （{summary['single_turn_total']} 条）"
    )
    print(
        f"  多轮  {summary['multi_turn_match_rate']:6.1%}"
        f"  （{summary['multi_turn_total']} 条，会话内按顺序执行，只评最后一轮）"
    )
    print(
        f"  多轮承接成功率 {summary['multi_turn_completion_rate']:6.1%}"
        f"  （{summary['multi_turn_completed']}/{summary['multi_turn_total']}"
        f" 条最后一轮没有落到拒答话术；计划形状对但执行没接上会在这里暴露）"
    )
    print(
        f"  澄清后可继续率 {summary['clarification_follow_up_rate']:6.1%}"
        f"  （{summary['clarification_followed_up']}/"
        f"{summary['clarification_follow_up_total']}"
        f" 条：上一轮提出澄清、员工照答之后是否真的完成了任务）"
    )
    print()
    print("=== 计划来源分布（意图被谁决定）===")
    for value, count in sorted(
        summary["plan_source_distribution"].items(), key=lambda item: -item[1]
    ):
        print(f"  {value or '(空)':<32} {count:>4}")
    failures = [item for item in scored if not item["passed"]]
    if failures:
        print()
        print(f"=== 未通过 {len(failures)} 条 ===")
        for item in failures:
            wrong = [
                key for key, ok in item["checks"].items() if not ok
            ]
            print(f"  [{','.join(wrong)}] {item['query'][:34]:<36}"
                  f" 期望={item['expected']['domain']}/{item['expected'].get('op')}"
                  f" 实际={item['actual']['domain']}/{item['actual']['op']}")
    if pending:
        print()
        print(f"=== 待业务决策 {len(pending)} 条（不计入指标）===")
        for item in pending:
            print(f"  {item['query'][:30]:<32} 实际={item['actual']['domain']}/"
                  f"{item['actual']['op']} 澄清={item['actual']['clarify']}")
    if real_data:
        print()
        print(f"=== 需真实资料验证 {len(real_data)} 条（不计入指标）===")
        print("  离线 fixture 的检索忽略查询词、对任何型号都返回同一产品，")
        print("  因此「换主体不得继承」只能在只读真实库评测里判定。")
        for item in real_data:
            print(f"  {' → '.join(item['turns'])}")
    print()
    print(f"报告：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
