"""图调用的共享实现：单轮与多轮、离线与只读真实库共用同一套身份与隔离逻辑。

抽出来的原因：`offline_env` 与 `readonly_env` 各自复制一份 `_patched` + 图调用，
再加多轮就是第四份。多轮必须共用同一个 user_id 与 thread_id，否则会话状态不传递，
这是会话评测正确性的关键，只应实现一次。
"""

from contextlib import contextmanager
from unittest.mock import patch

import app.business.service as service_module
from app.agent import RagAgent
from app.business.planner import TaskPlan
from app.business.service import BusinessAgent
from app.business.tools.common import RoutedReply
from app.config import Settings


@contextmanager
def patched_service(settings: Settings, knowledge: RagAgent, *, enable_planner: bool = False):
    """把 service 层的三个外部依赖替换为本进程内的隔离实现。

    `enable_planner=False`（默认）时 `_planner_client` 返回 None，整条链路退化为规则决策，
    结果确定、可重复、不花钱。这是评测的默认口径。

    `enable_planner=True` 时改用隔离的 RagAgent 提供模型客户端，于是**图执行路径真的会问模型**
    ——复现生产行为（生产没设 TASK_PLANNER_ENABLED，代码默认 True）必须打开它。
    此前这里写死返回 None，导致回放、两模式对照、计划评测全都只在无模型路径上跑过，
    生产带模型的那条路从未被这些评测覆盖。
    """
    planner = (lambda _settings: knowledge._llm_client()) if enable_planner else (
        lambda _settings: None
    )
    with (
        patch.object(service_module, "get_settings", lambda: settings),
        patch.object(service_module, "get_rag_agent", lambda: knowledge),
        patch("app.business.graph.builder._planner_client", planner),
    ):
        yield


class GraphSession:
    """在一组隔离依赖上执行单轮或多轮请求。"""

    def __init__(
        self,
        *,
        settings: Settings,
        knowledge: RagAgent,
        agent: BusinessAgent,
        scope: str,
        run_id: str,
        enable_planner: bool = False,
    ) -> None:
        self.settings = settings
        self.knowledge = knowledge
        self.agent = agent
        self.scope = scope
        self.run_id = run_id
        self.enable_planner = enable_planner

    def invoke_turns(
        self,
        index: int,
        queries: list[str],
        *,
        can_view_cost: bool = False,
    ) -> list[tuple[TaskPlan, RoutedReply]]:
        """在同一会话内按顺序执行多轮，返回每轮的 (计划, 回复)。

        多轮必须共用同一个 user_id 与 thread_id，否则上一轮选中的产品、待澄清槽位、
        话术会话都不会传递。不同 index 之间相互隔离，避免用例串味。
        """
        from app.business.graph import BusinessGraphContext
        from app.business.graph.identity import build_request_id, build_thread_id
        from app.business.graph.state import plan_from_payload, reply_from_payload

        user_id = f"{self.scope}-{self.run_id}-{index}"
        thread_id = build_thread_id(f"{self.scope}:{self.run_id}:{index}")
        turns: list[tuple[TaskPlan, RoutedReply]] = []
        with patched_service(
            self.settings, self.knowledge, enable_planner=self.enable_planner
        ):
            for turn_index, query in enumerate(queries):
                request_id = build_request_id(
                    f"{self.scope}:{self.run_id}:{index}:{turn_index}"
                )
                result = self.agent._get_business_graph().invoke(
                    {"request_id": request_id, "query": query},
                    config={"configurable": {"thread_id": thread_id}},
                    context=BusinessGraphContext(
                        agent=self.agent,
                        user_id=user_id,
                        can_view_cost=can_view_cost,
                    ),
                )
                turns.append(
                    (plan_from_payload(result["plan"]), reply_from_payload(result["reply"]))
                )
        return turns

    def invoke(
        self,
        index: int,
        query: str,
        *,
        can_view_cost: bool = False,
    ) -> tuple[TaskPlan, RoutedReply]:
        """执行一次请求，返回 (计划, 回复)。"""
        return self.invoke_turns(index, [query], can_view_cost=can_view_cost)[-1]
