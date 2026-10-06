"""Read-only replay of the reported two-turn product query against published data.

No live LLM, Redis, WeCom send, database initialization or production audit writes.
This checks execution semantics; it does not measure live planner accuracy.
"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from psycopg.conninfo import make_conninfo

from app.business.intent import Intent, IntentDecision
from app.business.product_query import ProductFieldRequest, ProductQuery, ProductReference
from app.business.service import BusinessAgent
from app.business.store import BusinessStore
from app.config import get_settings
from app.knowledge.store import psycopg_url


def main() -> int:
    parser = argparse.ArgumentParser(description="只读回放产品问答的 shadow 差异")
    parser.add_argument("--model", default="LY0653GDT-T")
    parser.add_argument("--output", type=Path, default=Path("outputs/product-shadow-replay.json"))
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("未配置数据库；未执行回放")
    connection = make_conninfo(
        psycopg_url(settings.database_url), options="-c default_transaction_read_only=on"
    )
    store = BusinessStore(connection)
    with patch("app.business.service.get_settings", return_value=SimpleNamespace(redis_url=None)):
        agent = BusinessAgent(store)
    agent._business_settings = lambda: SimpleNamespace(
        product_query_v2_mode="shadow", embedding_api_key=None, embedding_model=None
    )
    cases = []
    for suffix, evidence in (("主体有哪些材料组成?", "组成"), ("套装材料?", "套装材料")):
        message = args.model + suffix
        query = ProductQuery(
            fields=("material",),
            references=(ProductReference(args.model, "exact", args.model),),
            requested_fields=(ProductFieldRequest("material", evidence),),
        )
        decision = IntentDecision(Intent.PRODUCT, 1, "只读回放", message, source="planner_model")
        reply = agent._answer_product("replay-shadow", message, message, decision, query)
        cases.append(
            {
                "query": message,
                "delivered_content": reply.content,
                "diff": reply.execution_trace.get("shadow_diff", {}),
            }
        )
    checks = {
        "material_field_retained": cases[0]["diff"].get("v2_outcome", {}).get("requested_fields")
        == ["material"],
        "material_matches_published_data": cases[0]["diff"].get("same_semantics") is True,
        "set_material_clarifies": cases[1]["diff"].get("v2_outcome", {}).get("result_type")
        == "clarification",
        "set_material_does_not_query": not cases[1]["diff"].get("v2_execution"),
        "no_formal_v2_context": agent._state_get(agent._product_query_context, "replay-shadow")
        is None,
        "unseen_clarification_not_saved": agent._state_get(
            agent._product_shadow_query_context, "replay-shadow"
        )
        is None,
    }
    report = {
        "configured_mode": settings.product_query_v2_mode,
        "database_read_only": True,
        "planner": "deterministic_replay",
        "checks": checks,
        "passed": all(checks.values()),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": checks}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"回放失败：{type(exc).__name__}（连接信息已省略）") from None
