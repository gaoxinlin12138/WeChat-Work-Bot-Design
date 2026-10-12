"""Compare the current graph with the unified plan on the real catalog, read-only.

This is deliberately a side-by-side evaluator, not a production routing change.
The old graph and the new product bridge share the same read-only Store.  Policy
and script plans are reported as ``plan_only`` until their domain executors are
explicitly migrated.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from app.business.query_plan import (
    compare_shadow_plans,
    parse_query_plan,
    product_query_from_plan,
    query_plan_from_product_query,
)
from app.business.shadow import build_rule_shadow_plan
from scripts.readonly_env import ReadOnlyBusiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tmp" / "audit-export" / "queries.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "query-plan-readonly-report.json"


def _load(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _old_plan(task_plan):
    task = task_plan.tasks[-1] if task_plan.tasks else None
    if task is None or task.product_query is None:
        return None
    domain = "media" if str(task.decision.intent) == "product_media_query" else "product"
    return query_plan_from_product_query(task.product_query, domain=domain)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="真实业务库只读的新旧 QueryPlan 对比")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.corpus.exists():
        raise SystemExit(f"语料不存在：{args.corpus}，请先导出审计记录")

    business = ReadOnlyBusiness()
    try:
        value_catalog = business.agent.store.product_text_field_values()
    except Exception:
        value_catalog = {}
    results: list[dict[str, object]] = []
    for index, item in enumerate(_load(args.corpus), 1):
        query = str(item.get("query") or "")
        started = time.perf_counter()
        old_task, old_reply = business.invoke(index, query, can_view_cost=False)
        old_ms = (time.perf_counter() - started) * 1000
        old_plan = _old_plan(old_task)

        candidate = build_rule_shadow_plan(query)
        checked = parse_query_plan(candidate.to_dict(), query, value_catalog=value_catalog)
        new_reply = None
        new_ms = 0.0
        execution_mode = "plan_only"
        new_plan = candidate
        if checked.plan is not None:
            new_plan = checked.plan
        if checked.passed and new_plan.domain in {"product", "media"}:
            converted, bridge = product_query_from_plan(new_plan, query, can_view_sensitive=False)
            if converted is not None and not bridge.issues:
                started = time.perf_counter()
                if new_plan.domain == "product":
                    new_reply = business.agent._answer_product_v2(
                        f"readonly-new-{index}",
                        query,
                        converted,
                        can_view_cost=False,
                        cost_access_message="你当前没有查看成本价和采购价的权限。",
                    )
                execution_mode = (
                    "read_only_product_executor" if new_reply is not None else "plan_only_media"
                )
                new_ms = (time.perf_counter() - started) * 1000

        comparison = None
        expected_correction = False
        if old_plan is not None:
            comparison = compare_shadow_plans(
                str(item.get("case_id") or f"audit-{index}"),
                old_plan,
                new_plan,
                old_answer=str(old_reply.content),
                new_answer=str(new_reply.content) if new_reply is not None else "",
                latency_ms={"old": old_ms, "new": new_ms},
            )
            expected_correction = (
                old_plan.shape == "detail"
                and new_plan.shape == "detail_full"
                and new_reply is not None
                and str(old_reply.content) == str(new_reply.content)
            )
        results.append(
            {
                "case_id": item.get("case_id") or f"audit-{index}",
                "query": query,
                "recorded_intent": item.get("recorded_intent"),
                "old_reply": str(old_reply.content)[:1200],
                "new_reply": str(new_reply.content)[:1200] if new_reply is not None else None,
                "old_plan": old_plan.to_dict() if old_plan else None,
                "new_plan": new_plan.to_dict(),
                "new_validation": checked.to_dict(),
                "execution_mode": execution_mode,
                "expected_correction": expected_correction,
                "comparison": comparison.to_dict() if comparison else None,
            }
        )

    executable = [
        item for item in results if item["execution_mode"] == "read_only_product_executor"
    ]
    plan_only = [item for item in results if item["execution_mode"] != "read_only_product_executor"]
    shape_mismatches = [
        item
        for item in executable
        if item["comparison"]
        and not item["comparison"]["shape_match"]
        and not item["expected_correction"]
    ]
    report = {
        "schema_version": 1,
        "mode": "real-database-read-only-side-by-side",
        "read_only": True,
        "sent_messages": 0,
        "mutated_records": 0,
        "corpus": str(args.corpus.resolve().relative_to(ROOT)),
        "summary": {
            "total": len(results),
            "product_executor_comparisons": len(executable),
            "plan_only": len(plan_only),
            "shape_mismatches": len(shape_mismatches),
            "expected_corrections": sum(1 for item in results if item["expected_correction"]),
            "answer_differences": sum(
                1
                for item in results
                if item["comparison"] and not item["comparison"]["answer_match"]
            ),
            "unresolved_comparisons": sum(
                1
                for item in results
                if item["comparison"]
                and not item["comparison"]["passed"]
                and not item["expected_correction"]
            ),
            "validation_failures": sum(1 for item in results if not item["new_validation"]["pass"]),
        },
        "limitations": [
            "规则回放未启用大模型；这代表确定性降级路径，不代表线上 planner_model 路径。",
            "制度和话术只做统一计划审查，尚未执行新域执行器。",
            "答案文本可能因渲染细节不同而不同；事实一致性需要人工抽查差异项。",
            (
                "真实资料库当前只有已发布的实际产品；历史型号不存在时的 no_result "
                "是资料状态，不是计划错误。"
            ),
        ],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"只读新旧对比 {len(results)} 条 → {args.output}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
