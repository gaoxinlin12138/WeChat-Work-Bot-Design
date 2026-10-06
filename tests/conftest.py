"""Opt-in database tests create and drop only a fresh, isolated PostgreSQL schema."""

import os
from uuid import uuid4

import psycopg
import pytest
import redis
from psycopg import sql
from psycopg.conninfo import make_conninfo

# Tests must never inherit production authentication and route behavior from .env.
os.environ["APP_ENV"] = "test"

from app.config import get_settings
from app.knowledge.store import psycopg_url


@pytest.fixture(autouse=True)
def disable_live_background_jobs(monkeypatch):
    from app import main

    async def no_background_work():
        return None

    monkeypatch.setattr(main, "cleanup_product_trash", no_background_work)
    monkeypatch.setattr(main, "bootstrap_search_indexes", no_background_work)
    # A TestClient lifespan must never initialize the configured business store or
    # LangGraph checkpoint pool. Database tests opt in with an isolated schema.
    monkeypatch.setattr(main, "bootstrap_langgraph_checkpoint", no_background_work)
    # Default tests must never probe the configured Redis instance. Individual
    # tests that need a fake client can override this patch locally.
    def refuse_live_redis(*_args, **_kwargs):
        raise RuntimeError("测试环境禁止连接配置中的 Redis；请注入 fake client")

    monkeypatch.setattr(redis.Redis, "from_url", refuse_live_redis)


@pytest.fixture
def isolated_database_url():
    if os.environ.get("RUN_DATABASE_TESTS") != "1" and not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set RUN_DATABASE_TESTS=1 or TEST_DATABASE_URL for isolated PostgreSQL tests")
    database_url = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
    if not database_url:
        pytest.fail("PostgreSQL is not configured for integration tests", pytrace=False)
    schema = f"codex_test_{uuid4().hex}"
    # Never include connection strings or raw connection exceptions in test output.
    try:
        connection = psycopg.connect(psycopg_url(database_url), autocommit=True, connect_timeout=5)
    except psycopg.Error:
        pytest.fail("PostgreSQL connection failed (credentials omitted)", pytrace=False)
    with connection:
        extensions = connection.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm')"
        ).fetchall()
        if {row[0] for row in extensions} != {"vector", "pg_trgm"}:
            pytest.fail("Integration DB requires vector and pg_trgm extensions", pytrace=False)
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            # public is read for extension types/operators; unqualified CREATE/ALTER
            # targets the test schema. No production store initializer is invoked.
            yield make_conninfo(
                psycopg_url(database_url), options=f"-c search_path={schema},public"
            )
        finally:
            assert schema.startswith("codex_test_") and len(schema) == 43
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
