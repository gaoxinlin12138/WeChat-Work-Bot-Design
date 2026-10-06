from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.business import service as business_service
from app.business.graph.checkpoint import create_postgres_checkpointer
from app.business.store import BusinessStore
from app.knowledge.store import KnowledgeStore, psycopg_url


def test_application_store_factory_hard_refuses_configured_database_in_test(monkeypatch) -> None:
    monkeypatch.setattr(
        business_service,
        "get_settings",
        lambda: type("Settings", (), {"app_env": "test", "database_url": "configured"})(),
    )
    business_service.get_business_store.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="禁止通过应用工厂"):
            business_service.get_business_store()
    finally:
        business_service.get_business_store.cache_clear()


def test_postgresql_read_paths_match_current_schema(isolated_database_url, tmp_path) -> None:
    """在隔离 schema 验证真实 SQL，不让初始化或迁移改写生产表。"""
    business = BusinessStore(isolated_database_url)
    knowledge = KnowledgeStore(isolated_database_url, tmp_path)
    knowledge.initialize()
    business.initialize()

    assert isinstance(business.list_product_records(), list)
    assert isinstance(knowledge.list_documents(), list)


def test_concurrent_business_initialization_is_safe(isolated_database_url) -> None:
    stores = [BusinessStore(isolated_database_url) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda store: store.initialize(), stores))
    assert all(store._initialized for store in stores)
    assert stores[0].list_product_records() == []


def test_business_initialization_removes_legacy_table_and_expired_cache(
    isolated_database_url,
) -> None:
    with psycopg.connect(psycopg_url(isolated_database_url)) as connection:
        connection.execute(
            """CREATE TABLE business_write_requests (
                operation varchar(40) NOT NULL,
                request_key uuid NOT NULL,
                request_hash varchar(64) NOT NULL,
                resource_id uuid NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (operation, request_key)
            )"""
        )

    BusinessStore(isolated_database_url).initialize()
    with psycopg.connect(psycopg_url(isolated_database_url)) as connection:
        assert connection.execute(
            "SELECT to_regclass('business_write_requests')"
        ).fetchone()[0] is None
        connection.execute(
            """INSERT INTO wecom_media_cache
            (content_hash,media_type,media_id,expires_at)
            VALUES (%s,'image','expired',now()-interval '1 hour'),
                   (%s,'image','valid',now()+interval '1 day')""",
            ("a" * 64, "b" * 64),
        )

    BusinessStore(isolated_database_url).initialize()
    with psycopg.connect(psycopg_url(isolated_database_url)) as connection:
        rows = connection.execute(
            "SELECT media_id FROM wecom_media_cache ORDER BY media_id"
        ).fetchall()
    assert [row[0] for row in rows] == ["valid"]


def test_intent_audit_request_id_is_idempotent(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    values = {
        "user_id": "employee-1",
        "query": "你好",
        "intent": "conversation",
        "confidence": 1.0,
        "intent_source": "rules",
        "intent_reason": "greeting",
        "understood_query": "你好",
        "data_source": "none",
        "response_preview": "你好",
        "source_count": 0,
        "response_ms": 1.0,
        "request_id": "message-123",
    }

    first = store.record_intent_audit(**values)
    second = store.record_intent_audit(**values)

    assert first == second
    with store._connect() as connection:
        count = connection.execute("SELECT count(*) AS value FROM intent_audit_logs").fetchone()
    assert count["value"] == 1


def test_postgres_checkpointer_initializes_in_isolated_schema(isolated_database_url) -> None:
    resources = create_postgres_checkpointer(isolated_database_url, pool_size=2)
    try:
        with psycopg.connect(psycopg_url(isolated_database_url)) as connection:
            rows = connection.execute(
                """SELECT tablename FROM pg_tables
                WHERE schemaname = current_schema()
                  AND tablename LIKE 'checkpoint%'"""
            ).fetchall()
        assert {row[0] for row in rows} >= {"checkpoints", "checkpoint_writes"}
    finally:
        resources.close()


def test_postgres_checkpoint_survives_graph_and_pool_restart(isolated_database_url) -> None:
    class State(TypedDict):
        value: int

    def increment(state: State) -> dict[str, int]:
        return {"value": state["value"] + 1}

    def compile_graph(checkpointer):
        builder = StateGraph(State)
        builder.add_node("increment", increment)
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        return builder.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "checkpoint-restart-test"}}
    first_resources = create_postgres_checkpointer(isolated_database_url, pool_size=2)
    try:
        first_graph = compile_graph(first_resources.saver)
        assert first_graph.invoke({"value": 41}, config=config)["value"] == 42
    finally:
        first_resources.close()

    second_resources = create_postgres_checkpointer(isolated_database_url, pool_size=2)
    try:
        restarted_graph = compile_graph(second_resources.saver)
        assert restarted_graph.get_state(config).values["value"] == 42
    finally:
        second_resources.close()


def test_permanent_script_delete_handles_history_and_deleted_group(
    isolated_database_url,
) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    original = store.create_script(
        title="隔离测试案例",
        scenario="产品咨询",
        audience="customer",
        content="第一版回复",
        customer_question="第一版问题",
        employee_reply="第一版回复",
        source_row=7,
    )
    published = store.publish_script(str(original["id"]))
    assert published is not None
    latest = store.create_script_version(
        str(published["id"]),
        {
            "customer_question": "第二版问题",
            "employee_reply": "第二版回复",
            "content": "第二版回复",
        },
    )
    assert latest is not None
    current = store.publish_script(str(latest["id"]))
    assert current is not None

    # 历史版本在回收站、同组仍有使用中版本时，只清理选中的历史版本。
    assert store.permanently_delete_script(str(original["id"])) is True
    remaining = store.list_reply_scripts(include_deleted=True)
    assert [str(item["id"]) for item in remaining] == [str(current["id"])]

    # 整组移入回收站后，逻辑组 ID 也能定位并永久删除整组。
    assert store.delete_script(str(current["id"])) is not None
    assert store.permanently_delete_script(str(original["logical_id"])) is True
    assert store.list_reply_scripts(include_deleted=True) == []


def test_product_draft_edits_stay_out_of_published_projection(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    values = {
        "model": "SNAPSHOT-001",
        "series": "测试系列",
        "product_description": "测试产品",
        "set_price": 100,
        "body_price": 80,
        "components": [],
    }
    product = store.create_product_record(values)
    store.set_product_status(product["id"], "active")
    store.create_product_media(
        product["id"],
        role="body_image",
        original_name="draft-only.png",
        stored_path="data/product_media/draft-only.png",
        media_type="image",
        content_hash="c" * 64,
    )

    changed = dict(values, set_price=200, body_price=180)
    store.update_product_record(product["id"], changed)

    draft = store.get_product_record(product["id"])
    published = store.get_active_product_record(product["id"])
    assert draft["set_price"] == 200
    assert published["set_price"] == 100
    assert published["media"] == []
    assert "套装价：100.00元" in store.build_product_search_text(product["id"])
    assert "套装价：100.00元" in store.search_products("SNAPSHOT-001")[0].content

    store.set_product_status(product["id"], "active")
    assert store.get_active_product_record(product["id"])["set_price"] == 200


def test_published_media_survives_draft_replacement_until_publish(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    product = store.create_product_record({"model": "MEDIA-SNAPSHOT-001"})
    first = store.create_product_media(
        product["id"],
        role="product_image",
        original_name="published.png",
        stored_path="data/product_media/published.png",
        media_type="image",
        content_hash="d" * 64,
    )
    store.set_product_status(product["id"], "active")
    replacement = store.create_product_media(
        product["id"],
        role="product_image",
        original_name="draft.png",
        stored_path="data/product_media/draft.png",
        media_type="image",
        content_hash="e" * 64,
    )

    active = store.get_active_product_record(product["id"])
    assert [item["id"] for item in active["media"]] == [first["id"]]
    assert store.get_product_media(product["id"], first["id"]) is None
    store.set_product_status(product["id"], "active")
    active = store.get_active_product_record(product["id"])
    assert [item["id"] for item in active["media"]] == [replacement["id"]]
    assert "data/product_media/published.png" not in store.list_product_media_storage_paths()


def test_product_permanent_delete_rechecks_state_and_cutoff(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    product = store.create_product_record({"model": "TRASH-RACE-001"})
    assert store.delete_product_record(product["id"]) is True
    deleted = store.get_product_record(product["id"])
    deleted_at = deleted["deleted_at"]
    assert store.restore_product_record(product["id"])["status"] == "draft"
    assert store.permanently_delete_product_record(product["id"]) is None
    assert store.delete_product_record(product["id"]) is True
    assert (
        store.permanently_delete_product_record(
            product["id"], expected_deleted_at=deleted_at
        )
        is None
    )
    assert store.get_product_record(product["id"]) is not None


def test_restore_script_only_restores_requested_deleted_version(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    first = store.create_script(
        title="版本恢复测试",
        scenario="产品咨询",
        audience="customer",
        content="第一版",
        customer_question="第一版问题",
        employee_reply="第一版",
    )
    store.publish_script(str(first["id"]))
    second = store.create_script_version(
        str(first["id"]),
        {"content": "第二版", "customer_question": "第二版问题", "employee_reply": "第二版"},
    )
    store.publish_script(str(second["id"]))
    store.delete_script(str(second["id"]))

    restored = store.restore_script(str(second["id"]))
    assert restored is not None
    versions = store.list_reply_scripts(include_deleted=True)
    current = [item for item in versions if item["status"] == "published"]
    assert [str(item["id"]) for item in current] == [str(second["id"])]
