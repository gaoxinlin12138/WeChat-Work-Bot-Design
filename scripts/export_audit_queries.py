"""把 intent_audit_logs 里的真实员工提问导出为回放语料。

只读脚本：只执行 SELECT，不建表、不迁移、不写入任何表。

产出物含**真实员工提问**，虽然字段名叫 `query_redacted`，但
`normalize_audit_text` 只压缩空白、并未脱敏，所以导出内容就是原话。
因此默认输出到 gitignore 的 tmp/ 下，不要提交进版本库。

用法：
    python -m scripts.export_audit_queries
    python -m scripts.export_audit_queries --output tmp/audit-export/queries.jsonl
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tmp" / "audit-export" / "queries.jsonl"

FIELDS = (
    "created_at",
    "query_redacted",
    "intent",
    "confidence",
    "intent_source",
    "data_source",
    "source_count",
    "response_ms",
    "response_preview",
    "corrected_intent",
)


def database_url() -> str:
    for line in (ROOT / ".database.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            value = line.split("=", 1)[1].strip()
            return value.replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("未在 .database.env 中找到 DATABASE_URL")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="导出真实提问作为回放语料")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    columns = ", ".join(FIELDS)
    with psycopg.connect(database_url()) as connection:
        rows = connection.execute(
            f"SELECT {columns} FROM intent_audit_logs ORDER BY created_at"
        ).fetchall()

    # 历史记录只是当时的系统行为，不是正确答案：同一条问题可能已经在新代码里修好。
    # 因此字段一律以 recorded_ 前缀命名，避免被误当成标注真值。
    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(str(row[1]), []).append(row)

    corpus: list[dict[str, object]] = []
    for index, (query, occurrences) in enumerate(
        sorted(grouped.items(), key=lambda item: item[1][0][0]), 1
    ):
        latest = occurrences[-1]
        corpus.append(
            {
                "case_id": f"audit-{index:04d}",
                "query": query,
                "seen_count": len(occurrences),
                "recorded_at": latest[0].isoformat(timespec="seconds"),
                "recorded_intent": latest[2],
                "recorded_source": latest[4],
                "recorded_data_source": latest[5],
                "recorded_source_count": latest[6],
                "recorded_response_ms": round(float(latest[7]), 1),
                "recorded_preview": latest[8],
                "recorded_corrected_intent": latest[9],
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for item in corpus:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"审计记录 {len(rows)} 条 → 不重复提问 {len(corpus)} 条")
    print(f"已写入 {args.output}")
    print()
    print("=== 历史意图分布（当时的系统判断，非真值）===")
    for value, count in Counter(item["recorded_intent"] for item in corpus).most_common():
        print(f"  {value:<24} {count}")
    print()
    print("=== 历史决策来源分布 ===")
    for value, count in Counter(item["recorded_source"] for item in corpus).most_common():
        print(f"  {value:<32} {count}")
    rule_driven = sum(
        count
        for value, count in Counter(item["recorded_source"] for item in corpus).items()
        if not value.startswith("model") and value != "planner_model"
    )
    print(f"  规则/状态驱动合计：{rule_driven} / {len(corpus)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
