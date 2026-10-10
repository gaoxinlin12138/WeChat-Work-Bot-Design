"""Evaluate retrieval against the real published policy PDFs without generating answers."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent import get_rag_agent
from app.knowledge.store import SearchResult

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "policy_pdf_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "outputs" / "policy_pdf_eval_report.json"


@dataclass(frozen=True)
class PolicyPdfCase:
    case_id: str
    query: str
    expected_title_keyword: str
    expected_pages: list[int]
    expected_content_keywords: list[str]
    expected_element_type: str | None = None


def load_cases(path: Path) -> list[PolicyPdfCase]:
    return [
        PolicyPdfCase(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def result_matches(case: PolicyPdfCase, result: SearchResult) -> bool:
    content = _normalized(result.content)
    return (
        _normalized(case.expected_title_keyword) in _normalized(result.title)
        and result.page_number in case.expected_pages
        and all(_normalized(keyword) in content for keyword in case.expected_content_keywords)
        and (
            case.expected_element_type is None
            or result.element_type == case.expected_element_type
        )
    )


def evaluate(cases: list[PolicyPdfCase], *, limit: int, agent: Any = None) -> dict[str, Any]:
    if agent is None:
        agent = get_rag_agent()
    details: list[dict[str, Any]] = []
    reciprocal_rank = 0.0
    table_cases = 0
    table_hits = 0
    for case in cases:
        results = agent.search(case.query, limit=limit)
        rank = next(
            (
                index
                for index, result in enumerate(results, start=1)
                if result_matches(case, result)
            ),
            None,
        )
        if rank:
            reciprocal_rank += 1 / rank
        if case.expected_element_type == "table":
            table_cases += 1
            table_hits += int(rank is not None)
        details.append(
            {
                "case": asdict(case),
                "passed": rank is not None,
                "rank": rank,
                "results": [
                    {
                        "title": result.title,
                        "page_number": result.page_number,
                        "printed_page": result.printed_page,
                        "element_type": result.element_type,
                        "section_path": list(result.section_path),
                        "score": result.score,
                        "content_preview": result.content[:300],
                    }
                    for result in results
                ],
            }
        )
    hits = sum(item["passed"] for item in details)
    total = len(details)
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "real_published_policy_retrieval",
        "summary": {
            "total_cases": total,
            "passed_cases": hits,
            f"recall_at_{limit}": hits / total if total else 0.0,
            "mean_reciprocal_rank": reciprocal_rank / total if total else 0.0,
            "table_cases": table_cases,
            "table_hits": table_hits,
            "table_hit_rate": table_hits / table_cases if table_cases else 0.0,
        },
        "failures": [item["case"]["case_id"] for item in details if not item["passed"]],
        "results": details,
        "limitations": [
            "本评测检查真实已发布制度的检索、文档、页码、内容与表格命中，不调用答案生成模型。",
            "回答忠实度与逐句引用准确率需要在检索基线稳定后另行评测。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "通过只读会话运行：不调用 initialize()、不写审计、数据库层只读。"
            "检索质量与默认方式一致，但需要设置 RUN_READONLY_DATABASE_EVAL=1。"
        ),
    )
    args = parser.parse_args()
    agent = None
    if args.read_only:
        from scripts.readonly_env import ReadOnlyBusiness

        # with_embeddings 让向量检索用真实 embedding 配置，否则会退化为纯关键词检索。
        agent = ReadOnlyBusiness(with_embeddings=True).knowledge
    report = evaluate(load_cases(args.cases), limit=args.limit, agent=agent)
    report["mode"] = (
        "real_published_policy_retrieval_read_only"
        if args.read_only
        else "real_published_policy_retrieval"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
