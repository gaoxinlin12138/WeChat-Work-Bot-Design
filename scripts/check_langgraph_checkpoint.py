"""Verify PostgreSQL checkpoint persistence using only an explicit test database."""

import os
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.business.graph.checkpoint import create_postgres_checkpointer


class CheckState(TypedDict):
    value: int


def increment(state: CheckState) -> dict[str, int]:
    return {"value": state["value"] + 1}


def compile_graph(checkpointer):
    builder = StateGraph(CheckState)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "拒绝使用应用 DATABASE_URL。请显式设置非生产 TEST_DATABASE_URL 后重试。"
        )
    config = {"configurable": {"thread_id": f"checkpoint-smoke-{uuid4().hex}"}}
    first = create_postgres_checkpointer(database_url, pool_size=2)
    try:
        graph = compile_graph(first.saver)
        result = graph.invoke({"value": 41}, config=config)
        if result["value"] != 42:
            raise RuntimeError("首次 checkpoint 写入结果不正确")
    finally:
        first.close()

    second = create_postgres_checkpointer(database_url, pool_size=2)
    try:
        snapshot = compile_graph(second.saver).get_state(config)
        if snapshot.values.get("value") != 42:
            raise RuntimeError("连接池重建后未恢复 checkpoint")
    finally:
        second.close()
    print("LangGraph PostgreSQL checkpoint 恢复验证通过。")


if __name__ == "__main__":
    main()
