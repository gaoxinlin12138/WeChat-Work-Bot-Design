"""只读的真实数据评测环境。

与 offline_env 的区别：这里连**真实业务库**，用来测量必须依赖真实资料的指标
（字段漏答率、越权率、换主体时的上下文继承）。因此有两条硬约束，缺一不可：

1. **数据库层只读。** DSN 带上 `default_transaction_read_only=on`，任何写入都会被
   PostgreSQL 拒绝（实测报 `ReadOnlySqlTransaction`），而不是依赖代码自觉。
2. **绝不调用 `initialize()`。** `BusinessStore.initialize()` 里有
   `DROP TABLE ... CASCADE` 迁移语句，在生产库上执行是破坏性的。

另外把审计写入替换为空操作：只读事务里写审计必然失败，留着只会制造噪音日志。
默认拒绝运行，必须显式设置环境变量，与项目既有的 RUN_DATABASE_TESTS 约定一致。
"""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import app.business.service as service_module
from app.agent import RagAgent
from app.business.service import BusinessAgent
from app.business.store import BusinessStore
from app.config import Settings
from app.knowledge.store import KnowledgeStore
from scripts.graph_session import GraphSession

ROOT = Path(__file__).resolve().parents[1]
GUARD_ENV = "RUN_READONLY_DATABASE_EVAL"


def _raw_database_url() -> str:
    for line in (ROOT / ".database.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("未在 .database.env 中找到 DATABASE_URL")


def read_only_url() -> str:
    base = _raw_database_url()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options=-c%20default_transaction_read_only%3Don"


def ensure_allowed() -> None:
    if os.environ.get(GUARD_ENV) != "1":
        raise SystemExit(
            "本评测会连接真实业务库（会话级只读）。确认要执行请先设置 "
            f"{GUARD_ENV}=1。"
        )


class ReadOnlyBusiness(GraphSession):
    """在真实资料的只读会话里执行请求，同时暴露计划与回复。"""

    def __init__(
        self,
        *,
        with_embeddings: bool = False,
        with_planner: bool = False,
    ) -> None:
        ensure_allowed()
        # with_embeddings 用于测量检索质量：向量检索必须用真实 embedding 配置，
        # 否则会退化成纯关键词检索，结果与线上不一致。此时从真实 .env 读配置，
        # 但仍然强制把 database_url 换成只读 DSN、redis_url 置空。
        #
        # with_planner 用于**忠实复现生产行为**：生产没有设 TASK_PLANNER_ENABLED，
        # 代码默认 True，因此线上是「规则先筛 + 模型兜底」。只读环境默认把它关掉
        # （为了不花钱、结果可复现），但那样复现出来的多轮行为与线上不同。
        if with_embeddings or with_planner:
            settings = Settings(
                _env_file=(".env", ".database.env"),
                database_url=read_only_url(),
                redis_url=None,
                # 真实 .env 开着 checkpoint，它会在启动时建表，被只读事务正确拦下。
                # 复现行为不需要 checkpoint，这里关掉。
                langgraph_checkpoint_enabled=False,
                task_planner_enabled=with_planner,
            )
        else:
            settings = Settings(
                _env_file=None,
                llm_api_key=None,
                llm_model=None,
                embedding_api_key=None,
                embedding_model=None,
                redis_url=None,
                database_url=read_only_url(),
                task_planner_enabled=False,
            )
        with patch.object(service_module, "get_settings", lambda: settings):
            knowledge = RagAgent(
                settings,
                KnowledgeStore(
                    settings.database_url,
                    settings.knowledge_upload_dir,
                    settings.embedding_dimensions,
                ),
            )
            store = BusinessStore(settings.database_url, settings.embedding_dimensions)
            agent = BusinessAgent(store)
        # 只读事务里写审计必然失败，直接置空，避免逐条产生失败日志。
        store.record_intent_audit = lambda **_kwargs: None
        super().__init__(
            settings=settings,
            knowledge=knowledge,
            agent=agent,
            scope="readonly",
            run_id=uuid4().hex,
            enable_planner=with_planner,
        )
