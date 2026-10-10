"""对照评测：shadow（当前生产配置）与 on（V2 交付）对同一批提问的作答差异。

切到 on 会不会变差，此前每次都要手写探针去比对。这个脚本把它变成可重复的指标：

- 一致率：两种模式渲染出的字段完全一致的比例；
- 退化：on 拒答而 shadow 不拒答，或 on 缺少 shadow 有的字段；
- 越权：无成本权限时出现了敏感字段的**数值**（不是标签——拒答文案里本来就有字段名）。

只读真实库，需要 RUN_READONLY_DATABASE_EVAL=1。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.readonly_env import ReadOnlyBusiness  # noqa: E402

DEFAULT_CASES = ROOT / "evals" / "mode_parity_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "mode_parity_report.json"

REFUSAL_MARKERS = (
    "请先提供",
    "没有找到",
    "没有已发布",
    "没有足够依据",
    "暂时无法",
    "暂时不可用",
    "无法可靠",
)
SENSITIVE_LABELS = ("成本价", "采购价")
# 字段行可能带括号，例如「配件明细（共15项）：」；取值也可能为空（明细写在后续行里），
# 所以取值部分必须允许零长度，否则这类字段会被当成没渲染。
FIELD_PATTERN = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,6})(?:（[^）]*）)?：([^\n｜|]*)")
# 清单页把型号写在编号行上（「1. LY0653GDT」），而不是「产品型号：」，必须单独识别，
# 否则「你们有哪些产品」会被误判成 on 漏答型号。
NUMBERED_MODEL = re.compile(r"^\s*\d+[.、]\s*([A-Za-z0-9][A-Za-z0-9.\-]*)", re.MULTILINE)
MISSING_MARKERS = ("未登记", "未提供")


def parse_fields(text: str) -> dict[str, str]:
    """把作答正文解析成「字段名 → 取值」。

    「资料中未登记」和「资料未登记」只是措辞不同，统一成一个占位符，
    否则每条含空字段的用例都会被算成不一致。
    """
    fields: dict[str, str] = {}
    for model in NUMBERED_MODEL.findall(str(text)):
        fields.setdefault("产品型号", model)
    for label, value in FIELD_PATTERN.findall(str(text)):
        cleaned = value.strip()
        fields[label] = "<未登记>" if any(m in cleaned for m in MISSING_MARKERS) else cleaned
    return fields


def satisfies(field: str, fields: dict[str, str]) -> bool:
    """必需字段命中判定：「配件」应当被「配件明细」满足。"""
    return any(key == field or key.startswith(field) for key in fields)


def sensitive_values(text: str) -> list[str]:
    """无权限时是否给出了敏感字段的数值（拒答文案里只有字段名，没有数值）。"""
    leaked: list[str] = []
    for label, value in parse_fields(text).items():
        if label in SENSITIVE_LABELS and re.search(r"\d", value) and value != "<未登记>":
            leaked.append(f"{label}={value}")
    return leaked


def run_case(business: ReadOnlyBusiness, index: int, case: dict[str, Any]) -> dict[str, Any]:
    turns = list(case.get("turns") or [case["query"]])
    # 多轮用例在同一会话内按顺序执行，只对照最后一轮：前面几轮是铺垫。
    session = business.invoke_turns(
        index, turns, can_view_cost=bool(case.get("can_view_cost", False))
    )
    _plan, reply = session[-1]
    body = str(reply.content)
    return {
        "sources": len(reply.sources),
        "refusal": any(marker in body for marker in REFUSAL_MARKERS),
        "denied": "权限" in body,
        "fields": parse_fields(body),
        "leaked": sensitive_values(body) if not case.get("can_view_cost") else [],
        "preview": body.strip().splitlines()[0][:70] if body.strip() else "",
    }


def evaluate(cases: list[dict[str, Any]], *, with_model: bool = False) -> dict[str, Any]:
    sessions = {
        mode: ReadOnlyBusiness(v2_mode=mode, with_planner=with_model)
        for mode in ("shadow", "on")
    }
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        shadow = run_case(sessions["shadow"], index, case)
        on = run_case(sessions["on"], index, case)
        accepted = set(case.get("accepted_missing", []))
        # accepted_missing 记录「已知且已接受」的设计差异（例如 on 不再渲染备注），
        # 这类缺失单独统计，不算退化。
        missing = sorted(
            key
            for key in shadow["fields"]
            if key not in on["fields"] and key not in accepted
        )
        differing = sorted(
            key
            for key in shadow["fields"]
            if key in on["fields"] and shadow["fields"][key] != on["fields"][key]
        )
        # 判定退化的基准是「员工问了什么」，不是 shadow 输出了什么。
        # 「0653 的材质」在 on 下只给材质是对的，shadow 把全部字段都倒出来反而是多余的；
        # 把 shadow 当金标准会把这类正确行为误判成退化。
        required = [field for field in case.get("required_fields", []) if field not in accepted]
        missing_required = [
            field for field in required if not satisfies(field, on["fields"])
        ]
        challenges: list[str] = []
        if on["leaked"]:
            challenges.append(f"越权:{on['leaked']}")
        if on["refusal"] and not shadow["refusal"]:
            challenges.append("on变拒答")
        if missing_required:
            challenges.append(f"on漏答必需字段:{missing_required}")
        # 无权限时不能只是「什么都不说」，必须明确告知没有权限，否则员工以为系统坏了。
        if case.get("must_deny") and not on["denied"]:
            challenges.append("缺少权限说明")
        # 已查明根因、尚未修复的差异：单独一档，仍会打印出来，但不算「新出现的退化」。
        # 这样这个评测既能当切换闸门，也能当变化探测器。
        known_gap = bool(case.get("known_open_gap")) and bool(challenges)
        results.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "turns": list(case.get("turns") or [case["query"]]),
                "is_multi_turn": bool(case.get("turns")),
                "can_view_cost": bool(case.get("can_view_cost", False)),
                "tags": case.get("tags", []),
                "required_fields": required,
                "accepted_missing_in_on": sorted(accepted),
                "shadow": shadow,
                "on": on,
                "missing_required_in_on": missing_required,
                "missing_in_on": missing,
                "differing": differing,
                "accepted_missing_observed": sorted(
                    key for key in shadow["fields"] if key not in on["fields"] and key in accepted
                ),
                "known_open_gap": known_gap,
                "matches": not challenges,
                "challenges": challenges,
            }
        )
    total = len(results)
    matched = sum(1 for item in results if item["matches"])
    multi_turn = [item for item in results if item["is_multi_turn"]]
    regressions = [
        item
        for item in results
        if item["challenges"] and not item["known_open_gap"]
    ]
    known_gaps = [item for item in results if item["known_open_gap"] and item["challenges"]]
    return {
        "schema_version": 1,
        "mode": "shadow_vs_on_read_only_database",
        "summary": {
            "total_cases": total,
            "matched_cases": matched,
            "parity_rate": matched / total if total else 0.0,
            "multi_turn_cases": len(multi_turn),
            "multi_turn_parity_rate": (
                sum(1 for item in multi_turn if item["matches"]) / len(multi_turn)
                if multi_turn
                else 1.0
            ),
            "regressions": len(regressions),
            "known_open_gaps": len(known_gaps),
            "leak_cases": sum(1 for item in results if item["on"]["leaked"]),
            "refusal_cases": sum(
                1
                for item in results
                if item["on"]["refusal"] and not item["shadow"]["refusal"]
            ),
            "accepted_missing_cases": sum(
                1 for item in results if item["accepted_missing_observed"]
            ),
        },
        "regressions": [item["case_id"] for item in regressions],
        "results": results,
        "limitations": [
            "退化判定以用例声明的 required_fields（员工实际问的字段）为基准，"
            "而不是以 shadow 输出了什么为基准：字段更少不代表更差。",
            "字段取值已把「未登记」的各种措辞归一，避免把措辞差异算成退化。",
            "accepted_missing 声明的字段缺失属于已确认的设计差异，单独统计。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--with-model",
        action="store_true",
        help=(
            "按生产配置跑（模型可用）。默认不调用模型，测的是降级路径；"
            "判断切换 V2 的真实影响必须加这个开关。"
        ),
    )
    args = parser.parse_args()
    cases = [
        json.loads(line)
        for line in args.cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = evaluate(cases, with_model=args.with_model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(
        f"两模式对照：{summary['total_cases']} 条，一致 {summary['matched_cases']} 条"
        f"（一致率 {summary['parity_rate']:.1%}）"
    )
    print(
        f"  其中多轮 {summary['multi_turn_cases']} 条，一致率"
        f" {summary['multi_turn_parity_rate']:.1%}"
    )
    print(
        f"  退化 {summary['regressions']} 条｜越权 {summary['leak_cases']} 条"
        f"｜on 变拒答 {summary['refusal_cases']} 条"
        f"｜已知设计差异 {summary['accepted_missing_cases']} 条"
        f"｜已知未修 {summary['known_open_gaps']} 条"
    )
    for item in report["results"]:
        if item["challenges"]:
            prefix = "已知未修：" if item["known_open_gap"] else ""
            mark = "[已知]" if item["known_open_gap"] else "[X]"
            print(f"  {mark} {item['query']}  → {prefix}{'；'.join(item['challenges'])}")
        elif item["accepted_missing_observed"]:
            print(f"  [~] {item['query']}  → 已知差异：{item['accepted_missing_observed']}")
    return 0 if not report["regressions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
