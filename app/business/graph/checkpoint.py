"""PostgreSQL checkpoint resources, created only when explicitly enabled."""

import atexit
from dataclasses import dataclass

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.knowledge.store import psycopg_url


@dataclass
class CheckpointResources:
    saver: PostgresSaver
    pool: ConnectionPool

    def close(self) -> None:
        if not self.pool.closed:
            self.pool.close()


def create_postgres_checkpointer(
    database_url: str,
    *,
    pool_size: int = 4,
) -> CheckpointResources:
    """Open a thread-safe pool and initialize LangGraph's checkpoint schema."""
    pool = ConnectionPool(
        conninfo=psycopg_url(database_url),
        min_size=1,
        max_size=pool_size,
        open=True,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    try:
        saver = PostgresSaver(pool)
        saver.setup()
    except Exception:
        pool.close()
        raise
    resources = CheckpointResources(saver=saver, pool=pool)
    atexit.register(resources.close)
    return resources
