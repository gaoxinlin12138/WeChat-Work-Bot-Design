"""离线评测共用的隔离环境。

所有理解层评测（回放、计划匹配）都必须跑在这里，原因有两条：

1. **不连业务数据库。** `get_business_store()` / `get_knowledge_store()` 都会调用
   `initialize()`，而 `BusinessStore.initialize()` 里有 `DROP TABLE ... CASCADE`
   迁移语句。评测绝不能碰生产库。
2. **不调模型。** `llm_api_key=None` 使 `_planner_client` 返回 None，整条链路退化为
   规则决策，结果确定、可逐次对比。

fixture 资料只有 1 个产品（LTA-822）和 4 份资料，所以**只能用来判定计划与路由**。
要注意它的两个已知失真：

- `SnapshotBusinessStore.search_products` **忽略查询词**，对任何型号都返回那一个产品，
  因此「换型号时不得继承上一轮主体」这类负向用例在这里测不出真实行为，必须标记
  `needs-real-data` 并交给 `readonly_env`。
- 只有一个产品，所以分页、多候选消歧也测不到。
"""

from unittest.mock import patch
from uuid import uuid4

import app.business.service as service_module
from app.agent import RagAgent
from app.business.service import BusinessAgent
from app.config import Settings
from scripts.evaluate_chain import SnapshotBusinessStore, SnapshotKnowledgeStore
from scripts.graph_session import GraphSession


def offline_settings(v2_mode: str = "shadow") -> Settings:
    """构造与本机 .env 完全隔离的配置。"""
    return Settings(
        _env_file=None,
        llm_api_key=None,
        llm_model=None,
        embedding_api_key=None,
        embedding_model=None,
        redis_url=None,
        database_url=None,
        task_planner_enabled=False,
        product_query_v2_mode=v2_mode,
    )


class OfflineBusiness(GraphSession):
    """在隔离环境里执行请求，同时暴露计划与回复。"""

    def __init__(self, v2_mode: str = "shadow") -> None:
        settings = offline_settings(v2_mode)
        knowledge = RagAgent(settings, SnapshotKnowledgeStore())
        with patch.object(service_module, "get_settings", lambda: settings):
            agent = BusinessAgent(SnapshotBusinessStore())
        super().__init__(
            settings=settings,
            knowledge=knowledge,
            agent=agent,
            scope="offline",
            run_id=uuid4().hex,
        )
