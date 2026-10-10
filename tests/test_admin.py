from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.business.router as business_router
import app.knowledge.router as knowledge_router
import app.main as main_module
from app.knowledge.store import Document
from app.main import app


def _classify_with_model(monkeypatch, payload: str) -> dict[str, object]:
    """用桩模型驱动管理台归类，便于验证模型输出的边界校验。"""
    completions = SimpleNamespace(
        create=lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )
    )
    agent = SimpleNamespace(
        _llm_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    monkeypatch.setattr(
        business_router, "get_settings", lambda: SimpleNamespace(llm_api_key="k", llm_model="m")
    )
    monkeypatch.setattr(business_router, "get_rag_agent", lambda: agent)
    return business_router._classify_case("客户嫌贵", "我帮您申请一下")


def test_case_classification_rejects_out_of_set_model_values(monkeypatch) -> None:
    """提示词限定了 scenario 与 emotion 的取值，代码必须真的按枚举校验。

    此前模型返回「价格谈判」「愤怒」会被原样接受并写进管理台数据，
    按场景筛选时会静默漏掉这些行。
    """
    result = _classify_with_model(
        monkeypatch,
        '{"scenario":"价格谈判","intent":"想砍价","emotion":"愤怒","quality_score":0.5}',
    )

    assert result["scenario"] in business_router.CASE_SCENARIO_LABELS
    assert result["emotion"] in business_router.CASE_EMOTIONS
    # 越界时保留本地可解释的兜底值，而不是自由文本
    assert result["scenario"] == "报价议价"
    assert result["emotion"] == "平和"


def test_case_classification_accepts_in_set_model_values(monkeypatch) -> None:
    result = _classify_with_model(
        monkeypatch,
        '{"scenario":"售后投诉","intent":"投诉处理","emotion":"犹豫","quality_score":0.4}',
    )

    assert result["scenario"] == "售后投诉"
    assert result["emotion"] == "犹豫"
    assert result["quality_score"] == 0.4


def test_case_classification_keeps_valid_scenario_when_emotion_is_invalid(monkeypatch) -> None:
    result = _classify_with_model(
        monkeypatch,
        '{"scenario":"售后投诉","emotion":"愤怒","quality_score":9.5}',
    )

    assert result["scenario"] == "售后投诉"
    assert result["emotion"] == "平和"
    assert result["quality_score"] == 1.0


def test_admin_page_and_assets_are_available() -> None:
    client = TestClient(app)

    page = client.get("/admin")
    script = client.get("/admin-static/app.js")
    product_script = client.get("/admin-static/product-admin.js")

    assert page.status_code == 200
    assert "知识运营中枢" in page.text
    assert script.status_code == 200
    assert "loadDocuments" in script.text
    assert "替换文件" in script.text
    assert "previewDocument" in script.text
    assert "修改名称/分类" in script.text
    assert "showVersions" not in script.text
    assert "重新生成向量" in script.text
    assert "bulkDeleteDocuments" in script.text
    assert "/knowledge/documents/bulk-trash" in script.text
    assert "bulkDeleteScripts" in script.text
    assert "/business/scripts/bulk-trash" in script.text
    assert "bulkTrashWithFallback" in script.text
    assert "[404,405]" in script.text
    assert "重新解析并切分" in script.text
    assert "/reprocess" in script.text
    assert script.text.count("function setDocumentModule(") == 1
    assert script.text.count("function renderDocuments(") == 1
    assert "loadDocumentsBase" not in script.text
    assert "原文件匹配" in script.text
    documents_markup = page.text.split('id="documents"', 1)[1].split(
        'id="scripts"', 1
    )[0]
    for removed_field in (
        'name="product_name"',
        'name="model"',
        'name="document_date"',
        'name="version"',
        'name="tags"',
        'name="permissions"',
    ):
        assert removed_field not in documents_markup
    openapi = app.openapi()
    removed_api_fields = {
        "product_name",
        "model",
        "document_date",
        "version",
        "tags",
        "permissions",
    }
    for path in ("/knowledge/documents", "/knowledge/documents/{document_id}/replacement"):
        operation = openapi["paths"][path]["post"]
        schema_ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"][
            "$ref"
        ]
        schema_name = schema_ref.rsplit("/", 1)[-1]
        properties = openapi["components"]["schemas"][schema_name]["properties"]
        assert removed_api_fields.isdisjoint(properties)
    assert product_script.status_code == 200
    assert "/business/products/import-xlsx" in product_script.text
    assert "editCatalog" in product_script.text
    assert "deleteCatalog" in product_script.text
    assert "bulkDeleteCatalog" in product_script.text
    assert "/business/products/bulk-trash" in product_script.text
    assert "restoreCatalog" in product_script.text
    assert "permanentlyDeleteCatalog" in product_script.text
    assert "deleteScript" in script.text
    assert "restoreScript" in script.text
    assert "permanentlyDeleteScript" in script.text
    assert "disableScript" not in script.text
    assert (
        "document.createElement('details')"
        not in script.text.split("function ensureDocumentModules", 1)[1].split(
            "function setCatalogModule", 1
        )[0]
    )
    assert '<details class="catalog-module"' not in page.text
    scripts_markup = page.text.split('id="scripts"', 1)[1].split('id="catalog"', 1)[0]
    assert "已停用" not in scripts_markup
    assert 'id="modal"' in page.text
    assert 'id="product-import-form"' in page.text
    assert "Excel 批量导入" in page.text
    assert "单条建档" in page.text
    assert '<span>导入产品档案</span>' not in page.text
    assert "成本价（元）" in page.text
    assert 'name="piece_count"' in page.text
    assert 'id="piece-count-help"' in page.text
    assert '<span>价格</span><span>件数</span><span>资料</span>' in page.text
    assert "产品资料" in page.text
    assert 'id="product-form"' in page.text
    assert 'id="bulk-delete-scripts"' in page.text
    assert 'id="bulk-delete-products"' in page.text
    assert 'id="select-all-scripts"' in page.text
    assert 'id="select-all-products"' in page.text
    assert "全选当前结果" not in page.text
    assert 'class="library-table-shell script-library-table"' in page.text
    assert 'class="library-table-shell product-library-table"' in page.text
    assert "配置图" in page.text
    assert "产品图" in page.text
    assert "主体图" in page.text
    for component in ("淋浴杆", "装饰盖*2", "弯角*2", "橡皮*4", "止水阀*2", "珍珠棉袋"):
        assert component in product_script.text
    for removed_component in ("直管", "七字管", "六分橡皮", "膨胀螺丝", "珍珠棉带"):
        assert removed_component not in product_script.text
    assert 'name="style"' not in page.text
    product_properties = app.openapi()["components"]["schemas"]["ProductRecordUpsert"][
        "properties"
    ]
    assert "style" not in product_properties
    product_form = page.text.split('id="product-form"', 1)[1].split("</form>", 1)[0]
    ordered_fields = (
        "sequence_number",
        "model",
        "series",
        "product_description",
        "material",
        "spray_modes",
        "color",
        "special_function",
        "shipping_origin",
        "set_price",
        "cost_price",
        "body_price",
        "piece_count",
        "supplier",
        "body_supplier",
        "notes",
    )
    positions = [product_form.index(f'name="{field}"') for field in ordered_fields]
    assert positions == sorted(positions)
    assert (
        'name="product_video_upload" type="file" accept=".mp4,video/mp4" multiple'
        in page.text
    )
    assert "可多选 MP4 · 每个不超过9MB" in page.text
    assert "主图文件" not in page.text
    assert "实物图文件" not in page.text
    assert "视频文件" not in page.text
    assert "保存并发布" in page.text
    assert "回收站" in page.text
    assert 'class="toolbar-actions audit-toolbar"' in page.text
    assert ".catalog-command .form-title p" in client.get("/admin-static/style.css").text


def test_bulk_document_trash_route_uses_validated_ids(monkeypatch) -> None:
    selected = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]

    class FakeStore:
        def delete_documents(self, document_ids: list[str]) -> int:
            assert document_ids == selected
            return len(document_ids)

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", lambda: FakeStore())
    response = TestClient(app).post(
        "/knowledge/documents/bulk-trash", json={"document_ids": selected}
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}


def test_bulk_business_trash_routes_use_validated_ids(monkeypatch) -> None:
    selected = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]

    class FakeStore:
        def delete_scripts(self, script_ids: list[str]) -> int:
            assert script_ids == selected
            return 2

        def delete_product_records(self, product_ids: list[str]) -> int:
            assert product_ids == selected
            return 1

    monkeypatch.setattr(business_router, "get_business_store", lambda: FakeStore())
    client = TestClient(app)

    scripts = client.post("/business/scripts/bulk-trash", json={"script_ids": selected})
    products = client.post("/business/products/bulk-trash", json={"product_ids": selected})

    assert scripts.status_code == 200
    assert scripts.json() == {"deleted": 2}
    assert products.status_code == 200
    assert products.json() == {"deleted": 1}


def test_bulk_business_trash_routes_reject_empty_or_oversized_batches() -> None:
    client = TestClient(app)
    oversized = [f"00000000-0000-0000-0000-{index:012d}" for index in range(101)]

    assert client.post("/business/scripts/bulk-trash", json={"script_ids": []}).status_code == 422
    assert (
        client.post("/business/products/bulk-trash", json={"product_ids": oversized}).status_code
        == 422
    )


def test_production_admin_supports_browser_basic_auth(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(app_env="production", admin_api_key="secret"),
    )
    client = TestClient(app)

    denied = client.get("/admin")
    allowed = client.get("/admin", auth=("admin", "secret"))
    docs_denied = client.get("/docs")

    assert denied.status_code == 401
    assert "Basic" in denied.headers["WWW-Authenticate"]
    assert allowed.status_code == 200
    assert docs_denied.status_code == 401


def _document() -> Document:
    return Document(
        id="11111111-1111-1111-1111-111111111111",
        title="考勤管理制度",
        original_name="考勤制度.pdf",
        category="公司制度",
        status="published",
        visibility="public",
        size_bytes=1024,
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        published_at=datetime(2026, 9, 10, tzinfo=UTC),
        parse_error=None,
        embedding_status="ready",
        embedding_error=None,
        embedding_updated_at=datetime(2026, 9, 10, tzinfo=UTC),
        logical_id="11111111-1111-1111-1111-111111111111",
        version_number=1,
        is_current=True,
        supersedes_id=None,
    )


def test_document_preview_and_metadata_endpoints(monkeypatch) -> None:
    document = _document()

    class FakeStore:
        def document_chunks(self, document_id: str) -> list[dict[str, object]]:
            assert document_id == document.id
            return [{"chunk_index": 0, "page_number": 2, "content": "每日打卡"}]

        def update_metadata(self, document_id: str, title: str, category: str) -> Document:
            assert (document_id, title, category) == (
                document.id,
                "新版考勤制度",
                "人事制度",
            )
            return document

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", lambda: FakeStore())
    client = TestClient(app)

    preview = client.get(f"/knowledge/documents/{document.id}/chunks")
    update = client.patch(
        f"/knowledge/documents/{document.id}",
        json={"title": "新版考勤制度", "category": "人事制度"},
    )

    assert preview.json() == [{"chunk_index": 0, "page_number": 2, "content": "每日打卡"}]
    assert update.status_code == 200


def test_manual_document_maintenance_routes_are_removed() -> None:
    paths = app.openapi()["paths"]
    document_id = "{document_id}"
    assert f"/knowledge/documents/{document_id}/index" not in paths
    assert f"/knowledge/documents/{document_id}/reparse" not in paths
    assert f"/knowledge/documents/{document_id}/disable" not in paths
    assert f"/knowledge/documents/{document_id}/versions" not in paths
    assert "post" in paths[f"/knowledge/documents/{document_id}/replacement"]


def test_reply_script_disable_route_is_replaced_by_trash_routes() -> None:
    paths = app.openapi()["paths"]
    script_id = "{script_id}"
    assert f"/business/scripts/{script_id}/disable" not in paths
    assert "delete" in paths[f"/business/scripts/{script_id}"]
    assert "post" in paths[f"/business/scripts/{script_id}/restore"]
    assert "delete" in paths[f"/business/scripts/{script_id}/permanent"]
