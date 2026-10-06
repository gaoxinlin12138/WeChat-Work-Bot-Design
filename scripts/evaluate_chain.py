import argparse
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from openai import OpenAI

from app.agent import RagAgent
from app.business.chain_eval import ChainEvalCase, ChainReply, run_chain_evaluation
from app.business.intent import Intent, decide_intent, fallback_intent_decision
from app.business.service import BusinessAgent
from app.business.store import BusinessResult
from app.config import Settings, get_settings
from app.knowledge.store import SearchResult

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "chain_cases.jsonl"
OUTPUT = ROOT / "outputs" / "chain_eval_report.json"


def load_cases(path: Path) -> list[ChainEvalCase]:
    cases: list[ChainEvalCase] = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            value = json.loads(line)
            cases.append(
                ChainEvalCase(
                    case_id=str(value["case_id"]),
                    query=str(value["query"]),
                    expected_intent=Intent(str(value["expected_intent"])),
                    expected_data_source=str(value["expected_data_source"]),
                    expected_source_keywords=tuple(value.get("expected_source_keywords", [])),
                    expected_answer_keywords=tuple(value.get("expected_answer_keywords", [])),
                    citation_required=bool(value.get("citation_required", False)),
                    expect_no_evidence=bool(value.get("expect_no_evidence", False)),
                    expect_clarification=bool(value.get("expect_clarification", False)),
                    tags=tuple(value.get("tags", [])),
                )
            )
    return cases


class SnapshotKnowledgeStore:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.documents = [
            SearchResult(
                "doc-policy-attendance",
                "员工考勤制度",
                "制度",
                "1. 迟到一次扣除当日绩效。2. 员工应按规定打卡。",
                3,
                0.98,
            ),
            SearchResult(
                "doc-policy-leave",
                "员工请假制度",
                "制度",
                "2.1 年假应至少提前3个工作日申请。",
                5,
                0.96,
            ),
            SearchResult(
                "doc-expense",
                "费用报销制度",
                "制度",
                "3.2 出差打车费凭合规发票按流程报销。",
                8,
                0.94,
            ),
            SearchResult(
                "doc-company", "企业检测报告", "企业资料", "检测报告显示产品符合执行标准。", 2, 0.91
            ),
        ]

    def search(self, query: str, limit: int, query_embedding=None):
        if "不存在" in query or "9999" in query:
            return []
        terms = (
            ("迟到", "打卡")
            if "迟到" in query
            else ("年假",)
            if "年假" in query
            else ("报销", "出差")
            if "报销" in query
            else ("检测", "公司")
            if "检测" in query or "公司" in query
            else ()
        )
        found = [
            item
            for item in self.documents
            if terms and any(term in item.content or term in item.title for term in terms)
        ]
        return found[:limit]

    def add_message(self, user_id: str, role: str, content: str) -> None:
        self.messages.append((user_id, role, content))

    def recent_messages(self, user_id: str, limit: int):
        return []


class SnapshotBusinessStore:
    def __init__(self) -> None:
        self.products = [
            BusinessResult(
                "product",
                "product-lta-822",
                "LTA-822 · 灰色 / 全铝材质 / 方形",
                "产品型号：LTA-822；系列：太空铝系列；产品描述：套装花洒；"
                "款式：方形；材质：全铝材质；档位：4；颜色：灰色；"
                "出货地：浙江宁波；套装价：170.00元；主体价：110.00元；"
                "价格口径：不含税、不含运费。",
                0.99,
            )
        ]
        self.media = [
            BusinessResult(
                "media",
                "media-main",
                "LTA-822产品图",
                "LTA-822 产品图",
                0.98,
                "image",
                "main.webp",
                "product_image",
            ),
            BusinessResult(
                "media",
                "media-gallery",
                "LTA-822主体图",
                "LTA-822 产品主体图",
                0.97,
                "image",
                "gallery.webp",
                "body_image",
            ),
            BusinessResult(
                "media",
                "media-video",
                "LTA-822产品视频",
                "LTA-822 产品视频",
                0.96,
                "video",
                "install.mp4",
                "product_video",
            ),
        ]

    def search_products(self, query, **kwargs):
        return [] if "不存在" in query or "9999" in query else self.products

    def get_active_product(self, product_id):
        return next((p for p in self.products if p.source_id == product_id), None)

    def list_active_products(self, limit=100):
        return self.products[:limit]

    def search_media(
        self,
        query,
        media_type,
        limit=10,
        related_product_id=None,
        media_role=None,
        include_all=False,
    ):
        results = [
            item for item in self.media if media_type is None or item.media_type == media_type
        ]
        if media_role:
            results = [item for item in results if item.media_role == media_role]
        return results[:limit]

    def media_for_product(self, product_id):
        return self.media

    def single_active_product_id(self):
        return "product-lta-822"

    def as_dict(self, item):
        return asdict(item)

    def search_scripts(self, *args, **kwargs):
        return []


def build_runner(model_mode: bool):
    client = None
    model = None
    if model_mode:
        live_settings = get_settings()
        if not (live_settings.llm_api_key and live_settings.llm_model):
            raise RuntimeError("模型评测需要配置 LLM_API_KEY 和 LLM_MODEL")
        client = OpenAI(
            api_key=live_settings.llm_api_key,
            base_url=live_settings.llm_base_url,
            timeout=live_settings.embedding_timeout_seconds,
            max_retries=live_settings.embedding_max_retries,
        )
        model = live_settings.llm_model
    settings = Settings(
        _env_file=None,
        # 只把模型名交给规划图；回答生成保持关闭，确保评测资料和结果可复现。
        llm_api_key=None,
        llm_model=model,
        embedding_api_key=None,
        embedding_model=None,
        redis_url=None,
        database_url=None,
        task_planner_enabled=model_mode,
    )
    knowledge = RagAgent(settings, SnapshotKnowledgeStore())
    eval_run_id = uuid4().hex
    import app.business.service as service_module

    # 构造阶段就隔离 Redis 和数据库；退出评测后恢复依赖，避免污染同进程其他任务。
    with patch.object(service_module, "get_settings", lambda: settings):
        business = BusinessAgent(SnapshotBusinessStore())

    def evaluate_one(case: ChainEvalCase) -> ChainReply:
        decision = (
            decide_intent(case.query, client=client, model=model)
            if client and model
            else fallback_intent_decision(case.query)
        )
        with patch.object(service_module, "decide_intent", lambda *_args, **_kwargs: decision):
            routed = business.answer(
                f"intent-eval-{eval_run_id}-{case.case_id}",
                case.query,
                thread_id=f"chain-eval:{eval_run_id}:{case.case_id}",
                request_id=f"chain-eval:{eval_run_id}:{case.case_id}",
            )
        return ChainReply(
            routed.intent,
            routed.content,
            routed.sources,
            needs_clarification=decision.needs_clarification,
        )

    def reply_fn(case: ChainEvalCase) -> ChainReply:
        with (
            patch.object(service_module, "get_settings", lambda: settings),
            patch.object(service_module, "get_rag_agent", lambda: knowledge),
            patch("app.business.graph.builder._planner_client", lambda _settings: client),
        ):
            return evaluate_one(case)

    return reply_fn


def main() -> int:
    parser = argparse.ArgumentParser(description="评测意图到检索、回答的完整链路")
    parser.add_argument("--mode", choices=("fixture", "model"), default="fixture")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = run_chain_evaluation(
        load_cases(args.cases), build_runner(args.mode == "model"), mode=args.mode
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(f"完整链路评测：{summary['passed_cases']}/{summary['total_cases']} 项通过")
    print(
        f"来源命中率：{summary['source_hit_rate']:.2%}，引用准确率：{summary['citation_accuracy']:.2%}"
    )
    print(
        f"查无依据诚实率：{summary['no_evidence_honesty_rate']:.2%}，"
        f"平均/P95：{summary['average_latency_ms']:.0f}/{summary['p95_latency_ms']:.0f} ms"
    )
    print(f"失败用例：{len(report['failures'])}，报告：{args.output}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
