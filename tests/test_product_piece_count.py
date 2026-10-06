from io import BytesIO
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.business.router as router
from app.business.intent import Intent, classify_intent, is_product_catalog_query
from app.business.product_import import BASE_HEADERS, PRODUCT_HEADERS, parse_product_workbook
from app.business.product_selection import (
    has_product_identifier,
    is_product_follow_up,
    mentions_identifier_alias,
)
from app.business.service import BusinessAgent
from app.business.store import BusinessStore
from app.main import app


@pytest.mark.parametrize("count", [None, 0, 24, 2147483647])
def test_piece_count_create_api(monkeypatch, count):
    store = SimpleNamespace(create_product_record=lambda values: {"id": "test", **values})
    monkeypatch.setattr(router, "get_business_store", lambda: store)
    response = TestClient(app).post(
        "/business/products", json={"model": "TEST-COUNT", "piece_count": count}
    )
    assert response.status_code == 200
    assert response.json()["piece_count"] == count


@pytest.mark.parametrize("count", [-1, 0.5, True, "invalid", 2147483648])
def test_invalid_piece_count_never_reaches_store(monkeypatch, count):
    def unexpected_store():
        pytest.fail("Invalid count must be rejected before database access")

    monkeypatch.setattr(router, "get_business_store", unexpected_store)
    client = TestClient(app)
    for method, url in (("post", "/business/products"), ("put", "/business/products/test")):
        response = getattr(client, method)(url, json={"model": "TEST", "piece_count": count})
        assert response.status_code == 422


def count_workbook(count, column=10):
    headers = list(PRODUCT_HEADERS)
    product = [None] * len(headers)
    product[0:4] = [1, "TEST-COUNT", "测试系列", "测试产品"]
    product[9:12] = [170, 90, 110]
    product[len(BASE_HEADERS)] = "测试配件供应商"
    product[-1] = "原备注"
    prices = [None] * len(headers)
    prices[len(BASE_HEADERS) - 1] = "价格"
    prices[len(BASE_HEADERS)] = 1.25
    if column is not None:
        headers.insert(column, "件数")
        product.insert(column, count)
        prices.insert(column, None)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "产品档案"
    for row in (headers, product, prices):
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.mark.parametrize("column", [None, 0, 10, 34])
@pytest.mark.parametrize("count", [None, 0, 48])
def test_count_import_keeps_legacy_prices_and_components(column, count):
    record = parse_product_workbook(count_workbook(count, column))["records"][0]
    assert record["piece_count"] == (None if column is None else count)
    assert record["set_price"] == 170
    assert record["cost_price"] == 90
    assert record["body_price"] == 110
    assert record["components"][0]["supplier"] == "测试配件供应商"
    assert record["components"][0]["unit_price"] == 1.25
    assert record["notes"] == "原备注"


@pytest.mark.parametrize("count", [-1, 1.5, "NaN", "Infinity", True, 2147483648])
def test_count_import_rejects_invalid_counts(count):
    with pytest.raises(ValueError, match="件数"):
        parse_product_workbook(count_workbook(count))


@pytest.mark.parametrize(
    "query",
    [
        "件数呢",
        "库存多少",
        "还有多少",
        "还有多少件",
        "几件",
        "还剩多少",
        "配件价格",
        "配件总价是多少",
    ],
)
def test_count_queries_route_to_product_and_reuse_product_context(query):
    assert classify_intent(query) == Intent.PRODUCT
    assert is_product_catalog_query(query) is False
    assert is_product_follow_up(query) is True
    assert classify_intent("TEST-COUNT " + query) == Intent.PRODUCT


@pytest.mark.parametrize(
    "query",
    [
        "除了822，我们现在还有其他产品吗？",
        "除了LTA-822，我们现在还有其他产品吗？",
        "除LTA-822外还有别的产品吗",
    ],
)
def test_product_exclusion_queries_are_catalog_queries(query):
    assert is_product_catalog_query(query) is True


def test_product_model_suffix_is_a_supported_alias():
    assert mentions_identifier_alias("822图片发我", "LTA-822") is True
    assert mentions_identifier_alias("830图片发我", "LTA-822") is False


def test_product_model_before_compound_field_name_is_not_mistaken_for_quantity():
    assert has_product_identifier("LTA-822套装价多少") is True
    assert has_product_identifier("822件数还有多少") is True


def test_piece_count_api_publish_search_reply_and_lifecycle(isolated_database_url, monkeypatch):
    store = BusinessStore(isolated_database_url)
    store.initialize()
    monkeypatch.setattr(router, "get_business_store", lambda: store)
    monkeypatch.setattr(
        router, "get_business_agent",
        lambda: SimpleNamespace(index_product_record=lambda _id: None),
    )
    client = TestClient(app)
    created = client.post(
        "/business/products", json={"model": "TEST-COUNT", "piece_count": 120, "set_price": 170}
    )
    assert created.status_code == 200
    url = "/business/products/" + created.json()["id"]
    product_id = created.json()["id"]
    assert client.post(url + "/publish").status_code == 200

    updated = client.put(url, json={"model": "TEST-COUNT", "piece_count": 30})
    assert updated.json()["piece_count"] == 30
    assert updated.json()["published_snapshot"]["piece_count"] == 120
    assert store.get_active_product_record(product_id)["piece_count"] == 120
    assert "件数：120件" in store.search_products("TEST-COUNT库存")[0].content
    agent = BusinessAgent(store)
    reply = agent._format_product_detail(store.get_active_product(product_id), [], "库存多少")
    assert reply.content == "件数：120件"
    reply = agent._format_product_detail(
        store.get_active_product(product_id), [], "还有多少"
    )
    assert reply.content == "件数：120件"

    assert client.post(url + "/publish").json()["published_snapshot"]["piece_count"] == 30
    # An older client omitting the new optional field must not erase the stock count.
    assert client.put(url, json={"model": "TEST-COUNT"}).json()["piece_count"] == 30
    assert client.get(url).json()["piece_count"] == 30
    assert next(p for p in client.get("/business/products").json() if p["id"] == product_id)[
        "piece_count"
    ] == 30

    for count, expected in ((0, "0件"), (None, "资料中未登记")):
        response = client.put(url, json={"model": "TEST-COUNT", "piece_count": count})
        assert response.status_code == 200
        assert client.post(url + "/publish").status_code == 200
        reply = agent._format_product_detail(
            store.get_active_product(product_id), [], "件数和成本价"
        )
        assert f"件数：{expected}" in reply.content
        assert "没有查看成本价" in reply.content

    assert client.put(url, json={"model": "TEST-COUNT", "piece_count": 18}).status_code == 200
    client.post(url + "/publish")
    assert client.delete(url).status_code == 204
    restored = client.post(url + "/restore")
    assert restored.json()["piece_count"] == 18
    assert store.get_active_product_record(product_id)["piece_count"] == 18


def test_piece_count_migration_keeps_legacy_publication(isolated_database_url):
    store = BusinessStore(isolated_database_url)
    store.initialize()
    product = store.create_product_record({"model": "LEGACY-COUNT", "set_price": 170})
    store.set_product_status(product["id"], "active")
    with store._connect() as connection:
        assert connection.execute("SELECT current_schema() AS name").fetchone()["name"].startswith(
            "codex_test_"
        )
        connection.execute("ALTER TABLE product_records DROP COLUMN piece_count")
        connection.execute(
            "UPDATE product_records SET published_snapshot=published_snapshot-'piece_count'"
        )
    # Fresh startup migrates a pre-feature schema. A second startup is idempotent.
    for _ in range(2):
        migrated = BusinessStore(isolated_database_url)
        migrated.initialize()
        record = migrated.get_product_record(product["id"])
        assert record["piece_count"] is None
        assert record["status"] == "active"
        assert record["published_snapshot"]["set_price"] == 170
        assert migrated.get_active_product_record(product["id"])["piece_count"] is None
    with pytest.raises(psycopg.errors.CheckViolation), store._connect() as connection:
        connection.execute("UPDATE product_records SET piece_count=-1")


def test_style_column_is_removed_from_product_schema(isolated_database_url):
    store = BusinessStore(isolated_database_url)
    store.initialize()
    with store._connect() as connection:
        row = connection.execute(
            """SELECT 1 FROM information_schema.columns
            WHERE table_schema=current_schema()
              AND table_name='product_records' AND column_name='style'"""
        ).fetchone()
    assert row is None
