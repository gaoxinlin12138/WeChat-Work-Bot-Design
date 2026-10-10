"""测量产品问答的字段漏答率与越权率（对真实资料的只读评测）。

这两个指标无法用 fixture 判定：必须看真实资料里字段是否真的出现在回答中。

口径：

- **字段漏答率**：员工明确问到的字段，其标签没有出现在回答里的比例。
  只在「确实查到了产品」的用例上统计；查不到产品属于无结果，不是漏答。
  显式登记但值为空的字段会输出「资料中未登记」，这算答到，不算漏答。
- **越权率**：没有查看权限时，敏感字段（成本价/采购价）出现在回答里的用例比例。
  这是安全边界，必须为 0。
- 另设 `cost_visibility: allowed` 的用例验证反向：有权限且明确询问时必须给出，
  否则说明权限通路本身是坏的。

默认拒绝运行，需要显式设置 RUN_READONLY_DATABASE_EVAL=1。
"""

import argparse
import json
import re
import sys
from pathlib import Path

from app.business.product_query import PRODUCT_FIELD_CATALOG
from scripts.readonly_env import ReadOnlyBusiness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "plan_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "product_field_eval_report.json"

SENSITIVE_LABELS = ("成本价", "采购价")
# 「查到了产品」的判据：回答里出现了任一字段标签，说明走的是资料回答而非无结果话术。
NOT_FOUND_MARKERS = (
    "没有找到",
    "没有已发布",
    "请先提供",
    "暂时无法",
    "资料中未登记",
)


def label_of(field: str) -> str:
    return PRODUCT_FIELD_CATALOG[field].label


def field_present(field: str, text: str) -> bool:
    """字段是否出现在回答里。

    标签后面可能跟一段限定语：V2 输出的是「配件明细（共17项）：」而不是「配件：」，
    所以这里允许标签与冒号之间夹一段短限定语。漏答检查要宽松，否则会把答到的算成漏答。
    """
    return re.search(re.escape(label_of(field)) + r"[^：\n]{0,12}：", text) is not None


def sensitive_exposed(text: str) -> list[str]:
    """越权检查要严格：只认「成本价：」这种真正的取值输出。

    无权限时的提示语「你当前没有查看成本价和采购价的权限。」不含冒号取值形式，
    不能被算成泄漏。
    """
    return [label for label in SENSITIVE_LABELS if f"{label}：" in text]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="测量字段漏答率与越权率")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--v2-mode",
        choices=("shadow", "on"),
        default="shadow",
        help="shadow 交付旧链路答案；on 交付 V2 答案，用于比较两者",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help=(
            "按生产配置跑（模型可用）。不加时图执行路径不调用模型，测的是降级路径；"
            "生产没有设 TASK_PLANNER_ENABLED，代码默认 True，所以要判断线上真实表现必须加这个开关。"
        ),
    )
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in args.cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    product_cases = [
        case
        for case in cases
        if case["expected"]["domain"] == "product" and "policy-pending" not in case["tags"]
    ]

    business = ReadOnlyBusiness(v2_mode=args.v2_mode, with_planner=args.with_model)
    results: list[dict[str, object]] = []
    for index, case in enumerate(product_cases, 1):
        allowed = case.get("cost_visibility") == "allowed"
        # 每条用例都在「无成本权限」下跑一遍，用于越权检查；有权限用例另跑一遍。
        _, denied_reply = business.invoke(index, case["query"], can_view_cost=False)
        denied_text = str(denied_reply.content)
        leaked = sensitive_exposed(denied_text)

        expected_fields = list(case["expected"].get("fields", []))
        answered_text = denied_text
        missing: list[str] = []
        if allowed:
            _, allowed_reply = business.invoke(
                index * 1000, case["query"], can_view_cost=True
            )
            answered_text = str(allowed_reply.content)
            missing = [
                label_of(field)
                for field in expected_fields
                if not field_present(field, answered_text)
            ]
        elif expected_fields:
            # 无权限时敏感字段必然缺席，因此只统计非敏感字段。
            missing = [
                label_of(field)
                for field in expected_fields
                if not PRODUCT_FIELD_CATALOG[field].sensitive
                and not field_present(field, answered_text)
            ]

        results.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "expected_fields": expected_fields,
                "missing_fields": missing,
                "answered": not any(m in answered_text for m in NOT_FOUND_MARKERS),
                "leaked_sensitive_labels": leaked,
                "answer_preview": answered_text[:200],
            }
        )

    scored = [item for item in results if item["expected_fields"]]
    total_expected = sum(len(item["expected_fields"]) for item in scored)
    total_missing = sum(len(item["missing_fields"]) for item in scored)
    leaks = [item for item in results if item["leaked_sensitive_labels"]]

    summary = {
        "product_cases": len(results),
        "field_cases": len(scored),
        "expected_field_total": total_expected,
        "missing_field_total": total_missing,
        "field_omission_rate": (total_missing / total_expected if total_expected else 0.0),
        "sensitive_leak_cases": len(leaks),
        "sensitive_leak_rate": (len(leaks) / len(results) if results else 0.0),
    }

    report = {
        "schema_version": 1,
        "mode": f"rules-only-read-only-database-{args.v2_mode}",
        "limitations": [
            "离线规则路径：只代表模型不可用时的表现。",
            "只读会话执行：DSN 带 default_transaction_read_only=on，数据库会拒绝任何写入。",
            "依赖当前真实产品资料，产品被改动后结论需要重跑。",
            "字段标签是存在性判据；标签存在但值为「资料中未登记」仍算答到。",
        ],
        "summary": summary,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"产品用例 {summary['product_cases']} 条（其中 {summary['field_cases']} 条含字段期望）")
    print()
    print("=== 核心指标 ===")
    print(
        f"  字段漏答率    {summary['field_omission_rate']:6.1%}"
        f"   （{total_missing}/{total_expected} 个期望字段未出现）"
    )
    print(
        f"  越权率       {summary['sensitive_leak_rate']:6.1%}"
        f"   （{len(leaks)}/{len(results)} 条用例在无权限时泄漏敏感字段）"
    )
    if leaks:
        print()
        print("  !! 越权用例：")
        for item in leaks:
            print(f"     {item['query'][:40]}  泄漏={item['leaked_sensitive_labels']}")
    missed = [item for item in scored if item["missing_fields"]]
    if missed:
        print()
        print("=== 漏答明细 ===")
        for item in missed:
            print(
                f"  {item['query'][:38]:<40} 缺={item['missing_fields']}"
                f"  已答到产品={item['answered']}"
            )
    print()
    print(f"报告：{args.output}")
    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
