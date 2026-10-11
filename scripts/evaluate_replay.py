"""用真实员工提问回放当前理解层，得到可对比、可重复的基线。

设计要点：

- **不连接业务数据库、不调用模型。** 复用 evaluate_chain 的进程内隔离环境
  （fixture 资料 + 规则决策），因此结果是确定性的，可以逐次对比。
- **输出 `intent_source`。** 这是本工具的核心指标：真实提问里有多少比例由模型决定，
  有多少由关键字模板、状态推测或正则守卫决定。目标是把后者的占比降下来。
- **每条提问独立会话。** 审计表没有保存当时的对话上下文，所以多轮追问
  （「产品图”」「都可以。」）会在无上下文条件下回放，表现偏差属于预期，不是回归。
- 审计里的 `recorded_*` 字段仅作参考：**它是当时的系统行为，不是正确答案**。
  同一条提问可能已经被后续修复改好，必须先回放再判断。

用法：
    python -m scripts.export_audit_queries
    python -m scripts.evaluate_replay
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from app.business.product_selection import has_product_identifier, is_product_follow_up
from scripts.offline_env import OfflineBusiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tmp" / "audit-export" / "queries.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "replay_report.json"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="回放真实提问，测量理解层基线")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.corpus.exists():
        raise SystemExit(
            f"语料不存在：{args.corpus}\n请先运行 python -m scripts.export_audit_queries"
        )

    corpus = [
        json.loads(line)
        for line in args.corpus.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    business = OfflineBusiness()
    results: list[dict[str, object]] = []
    for index, item in enumerate(corpus, 1):
        query = str(item["query"])
        _, reply = business.invoke(index, query)
        results.append(
            {
                "case_id": item["case_id"],
                "query": query,
                "seen_count": item.get("seen_count", 1),
                "mentions_product_identifier": has_product_identifier(query),
                "needs_conversation_context": is_product_follow_up(query),
                "recorded_intent": item.get("recorded_intent"),
                "recorded_source": item.get("recorded_source"),
                "recorded_source_count": item.get("recorded_source_count"),
                "replayed_intent": str(reply.intent),
                "replayed_source": str(reply.intent_source),
                "replayed_reason": str(reply.intent_reason),
                "understood_query": str(reply.understood_query),
                "source_count": len(reply.sources),
                "content": str(reply.content),
            }
        )

    # 这个语料没有保存当时的对话上下文，而 fixture 又只有 1 个产品和 4 份资料，
    # 所以零来源必须分三类：只有「自足提问」这一类的比例才是可解释的指标。
    zero = [item for item in results if item["source_count"] == 0]
    zero_fixture = [item for item in zero if item["mentions_product_identifier"]]
    zero_context = [
        item
        for item in zero
        if not item["mentions_product_identifier"] and item["needs_conversation_context"]
    ]
    zero_standalone = [
        item
        for item in zero
        if not item["mentions_product_identifier"] and not item["needs_conversation_context"]
    ]
    standalone = [
        item
        for item in results
        if not item["mentions_product_identifier"] and not item["needs_conversation_context"]
    ]

    report = {
        "schema_version": 1,
        # argparse accepts both repository-relative and absolute paths.  Resolve
        # before relative_to so a real audit replay does not fail after all
        # queries have already been evaluated.
        "corpus": str(args.corpus.resolve().relative_to(ROOT)),
        "mode": "rules-only-offline",
        "limitations": [
            "回放使用进程内 fixture 资料（1 个产品、4 份资料），衡量的是理解与路由层，"
            "不是真实检索质量。",
            "未使用模型：回放的是降级路径，不代表线上由模型决策时的表现。",
            "语料没有保存当时的对话上下文。因此只有「自足提问」"
            "（既不提型号、也不是省略式追问）的零来源比例才可解释；"
            "zero_source_fixture_suspect 与 zero_source_needs_context 两类不计入指标。",
            "recorded_* 字段是历史系统行为，不是标注真值。",
            "本模式没有模型可用，因此「决策来源」全部是非模型来源——这是构造决定的，"
            "不是质量指标。有意义的是各确定性机制之间的分布，以及它与线上模型可用时的"
            "分布差异（线上模型可用时会多出 model / planner_model 两类）。",
        ],
        "summary": {
            "total": len(results),
            "distinct_answers": len({item["content"] for item in results}),
            "zero_source": len(zero),
            "zero_source_fixture_suspect": len(zero_fixture),
            "zero_source_needs_context": len(zero_context),
            "zero_source_standalone": len(zero_standalone),
            "standalone_total": len(standalone),
            "standalone_zero_source_rate": (
                len(zero_standalone) / len(standalone) if standalone else 0.0
            ),
            "decision_source_distribution": dict(
                Counter(item["replayed_source"] for item in results)
            ),
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    total = int(summary["total"])
    print(f"回放 {total} 条真实提问 → {args.output}")
    print()
    print("=== 决策来源（意图被哪个机制决定）===")
    for value, count in Counter(item["replayed_source"] for item in results).most_common():
        share = count / total
        print(f"  {value or '(空)':<32} {count:>4}  {share:6.1%}")
    print("  本模式无模型可用，因此不会有 model / planner_model，这是构造决定的。")
    print("  有意义的是各确定性机制之间的分布；与线上模型可用时的分布对比才有诊断价值。")
    print()
    print("=== 交付意图分布 ===")
    for value, count in Counter(item["replayed_intent"] for item in results).most_common():
        print(f"  {value:<24} {count:>4}")
    print()
    print(f"零来源回答：{summary['zero_source']}/{total}（{summary['zero_source'] / total:.1%}）")
    print(f"不同答案数：{summary['distinct_answers']}（越接近 1 越说明回答趋同）")
    print()
    print("=== 零来源回答（三类必须分开看）===")
    print(
        f"  提到真实型号、因 fixture 资料缺失："
        f"{summary['zero_source_fixture_suspect']:>3}  ← 不算理解层缺陷"
    )
    print(
        f"  省略式追问、语料缺对话上下文："
        f"{summary['zero_source_needs_context']:>3}  ← 无法用本语料判定"
    )
    print(f"  自足提问、属于理解/路由问题：{summary['zero_source_standalone']:>3}")
    print(
        f"  → 自足提问共 {summary['standalone_total']} 条，其中零来源 "
        f"{summary['zero_source_standalone']} 条"
        f"（{summary['standalone_zero_source_rate']:.1%}）"
    )
    print(
        "     注意：这是**原始**零来源率，不是缺陷率。它仍混有本身就该无来源的回答"
        "（问候、话术澄清）和 fixture 缺数据造成的空结果。"
    )
    print("     要得到缺陷率，必须先给这批提问标注期望计划（evals/plan_cases.jsonl）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
