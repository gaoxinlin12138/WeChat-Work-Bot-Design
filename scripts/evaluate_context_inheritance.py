"""测量多轮上下文继承：该继承的有没有继承，不该继承的有没有越界。

这是风险最高的一类失败。员工在第二轮明确换了一个型号，系统若继续拿上一轮的产品
作答，就会**用错型号的数据回答**。在报价场景里这是业务风险，不是体验问题。

为什么必须用真实资料：离线 fixture 的 `search_products` 忽略查询词、对任何型号都
返回同一个产品，所以在那里只能测出 fixture 的行为。因此本脚本走只读真实库，
并只评 `needs-real-data` 标记的多轮用例。

默认拒绝运行，需要显式设置 RUN_READONLY_DATABASE_EVAL=1。
"""

import argparse
import json
import sys
from pathlib import Path

from scripts.readonly_env import ReadOnlyBusiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "plan_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "context_inheritance_eval_report.json"

REAL_DATA_TAG = "needs-real-data"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="只读真实库上的多轮上下文继承评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    cases = [
        item
        for line in args.cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
        if REAL_DATA_TAG in item.get("tags", [])
    ]
    if not cases:
        raise SystemExit(f"没有找到标记 {REAL_DATA_TAG} 的用例")

    business = ReadOnlyBusiness()
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases, 1):
        turns = list(case.get("turns") or [case["query"]])
        session = business.invoke_turns(index, turns)
        _, reply = session[-1]
        answer = str(reply.content)
        forbidden = [
            item for item in case["expected"].get("must_not_contain", []) if item in answer
        ]
        results.append(
            {
                "case_id": case["case_id"],
                "turns": turns,
                "forbidden": case["expected"].get("must_not_contain", []),
                "forbidden_hits": forbidden,
                "answer": answer[:400],
            }
        )

    leaks = [item for item in results if item["forbidden_hits"]]
    summary = {
        "cases": len(results),
        "over_inherit_cases": len(leaks),
        "over_inherit_rate": len(leaks) / len(results) if results else 0.0,
    }

    report = {
        "schema_version": 1,
        "mode": "rules-only-read-only-database-v2",
        "limitations": [
            "离线规则路径：只代表模型不可用时的表现。",
            "只读会话执行：DSN 带 default_transaction_read_only=on。",
            "依赖当前真实产品资料，产品被改动后结论需要重跑。",
            "只检查「是否出现上一轮主体的特征值」；是否答对了新型号需要人工核对回答原文。",
        ],
        "summary": summary,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"多轮上下文继承评测：{summary['cases']} 条（只读真实库，V2 模式）")
    print()
    print(
        f"  上下文越界率  {summary['over_inherit_rate']:6.1%}"
        f"  （{len(leaks)}/{summary['cases']} 条把上一轮主体的特征值"
        "答进了新型号的回答）"
    )
    for item in results:
        flag = "FAIL" if item["forbidden_hits"] else "OK  "
        print()
        print(f"  [{flag}] {' → '.join(item['turns'])}")
        if item["forbidden_hits"]:
            print(f"         越界命中={item['forbidden_hits']}")
        print(f"         作答: {str(item['answer'])[:96]}")
    print()
    print(f"报告：{args.output}")
    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
