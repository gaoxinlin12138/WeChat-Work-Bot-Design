"""Run the unified QueryPlan offline replay without a database or transport."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.business.shadow import replay_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="evals/shadow_query_cases.jsonl")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = replay_file(Path(args.cases))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

