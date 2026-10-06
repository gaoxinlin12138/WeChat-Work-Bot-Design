from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

import app.knowledge.router as knowledge_router
from app.business.intent import Intent, IntentDecision
from app.business.tools.knowledge import KnowledgeToolMixin
from app.knowledge.processor import (
    ExtractedPage,
    chunk_pages,
    summarize_extraction,
)
from app.knowledge.service import (
    format_search_reply,
    format_source_citation,
    format_source_citations,
)
from app.knowledge.store import Document, KnowledgeStore, SearchResult, psycopg_url
from app.main import app


def test_chunk_pages_preserves_page_number_and_overlap() -> None:
    chunks = chunk_pages(
        [ExtractedPage("一二三四五六七八九十", 2)],
        max_chars=6,
        overlap_chars=2,
    )
    assert [chunk.content for chunk in chunks] == ["一二三四五六", "五六七八九十"]
    assert all(chunk.page_number == 2 for chunk in chunks)


def test_chunk_pages_prefers_clause_headings_and_keeps_context() -> None:
    page = ExtractedPage(
        "第六章 请假管理\n6.1 申请原则\n员工应提前提交申请。\n6.2 审批流程\n由直属主管审批。",
        4,
    )
    chunks = chunk_pages([page], max_chars=80, overlap_chars=10)
    assert len(chunks) == 2
    assert "第六章 请假管理 / 6.1 申请原则" in chunks[0].content
    assert "员工应提前提交申请" in chunks[0].content
    assert "第六章 请假管理 / 6.2 审批流程" in chunks[1].content
    assert all(chunk.page_number == 4 for chunk in chunks)
    assert chunks[0].section_path == ("第六章 请假管理", "6.1 申请原则")
    assert chunks[0].clause_numbers == ("6.1",)


def test_table_chunk_keeps_element_type_and_current_section() -> None:
    chunks = chunk_pages(
        [
            ExtractedPage("第六章 考勤管理\n6.1 工作时间\n员工按班次出勤。", 3),
            ExtractedPage(
                "表格（PDF第3页）\n第1行：岗位=财务；月休天数=1天/周",
                3,
                printed_page="2/7",
                element_type="table",
                metadata={"rows": 2, "columns": 2},
            ),
        ]
    )

    table = next(chunk for chunk in chunks if chunk.element_type == "table")
    assert table.section_path == ("第六章 考勤管理", "6.1 工作时间")
    assert table.printed_page == "2/7"
    assert table.metadata == {"rows": 2, "columns": 2}


def test_cross_page_continuation_inherits_previous_clause_context() -> None:
    chunks = chunk_pages(
        [
            ExtractedPage("6.10 握手礼仪\n6.10.1 行握手礼时应注视对方，距离约一", 6),
            ExtractedPage("步，握手时间3秒左右为宜。\n6.10.2 握手力度应适中。", 7),
        ],
        max_chars=100,
        overlap_chars=10,
    )

    continuation = next(chunk for chunk in chunks if "3秒" in chunk.content)
    assert continuation.page_number == 7
    assert "6.10.1 行握手礼时应注视对方" in continuation.content
    assert continuation.section_path == (
        "6.10 握手礼仪",
        "6.10.1 行握手礼时应注视对方，距离约一",
    )


def test_extraction_summary_routes_native_mixed_and_scanned_pages() -> None:
    native = ExtractedPage(
        "正文", 1, metadata={"needs_ocr": False, "low_quality": False, "image_count": 0}
    )
    scanned = ExtractedPage(
        "", 2, metadata={"needs_ocr": True, "low_quality": True, "image_count": 1}
    )
    table = ExtractedPage("表格", 1, element_type="table")

    assert summarize_extraction([native, table]) == {
        "document_type": "native_text_pdf",
        "page_count": 1,
        "needs_ocr_pages": [],
        "low_quality_pages": [],
        "image_pages": [],
        "table_pages": [1],
        "table_count": 1,
    }
    mixed = summarize_extraction([native, scanned])
    assert mixed["document_type"] == "mixed_pdf"
    assert mixed["needs_ocr_pages"] == [2]



def test_database_url_is_converted_for_psycopg() -> None:
    assert (
        psycopg_url("postgresql+psycopg://user:pass@localhost/db")
        == "postgresql://user:pass@localhost/db"
    )


def test_detection_report_search_uses_only_title_and_original_filename() -> None:
    document = Document(
        id="report-1",
        title="水龙头检测报告",
        original_name="BY297-report.pdf",
        category="检测报告",
        status="published",
        visibility="public",
        size_bytes=10,
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        parse_error=None,
        embedding_status="not_requested",
        embedding_error=None,
        embedding_updated_at=None,
        logical_id="report-1",
        version_number=1,
        is_current=True,
        supersedes_id=None,
        metadata={"product_name": "不应参与匹配", "model": "LEGACY-001"},
    )

    assert KnowledgeStore._report_search_values(document) == [
        "水龙头检测报告",
        "by297-report.pdf",
    ]


def test_search_reply_includes_source_and_page() -> None:
    result = SearchResult(
        document_id="id",
        title="检测报告",
        category="报告",
        content="材质符合相关检测要求。",
        page_number=3,
        score=0.8,
        chunk_index=4,
    )
    reply = format_search_reply("材质", [result])
    assert "材质符合" in reply
    assert "引用依据：\n- 【《检测报告》｜PDF第3页】" in reply
    assert format_source_citation(result) in reply


def test_source_citation_includes_clause_page_and_chunk() -> None:
    result = SearchResult(
        document_id="id",
        title="奖惩管理制度",
        category="公司制度",
        content="6.3 奖惩原则 6.3.1 以客观事实为依据。",
        page_number=4,
        score=0.8,
        chunk_index=2,
    )

    assert format_source_citation(result) == ("【《奖惩管理制度》｜第6.3、6.3.1条｜PDF第4页】")


def test_empty_search_reply_is_explicit() -> None:
    assert "暂未找到" in format_search_reply("不存在", [])


def test_source_citations_merge_chunks_from_the_same_page() -> None:
    results = [
        SearchResult("id", "考勤制度", "制度", "6.5.1 调休申请", 5, 0.8, 1),
        SearchResult("id", "考勤制度", "制度", "6.5.2 调休期限", 5, 0.7, 2),
        SearchResult("id", "考勤制度", "制度", "3.6 调休定义", 3, 0.6, 3),
    ]
    assert format_source_citations(results) == [
        "【《考勤制度》｜第6.5.1、6.5.2条｜PDF第5页】",
        "【《考勤制度》｜第3.6条｜PDF第3页】",
    ]


def test_retry_embedding_endpoint_reindexes_published_document(monkeypatch) -> None:
    document = Document(
        id="doc-1",
        title="制度",
        original_name="制度.pdf",
        category="制度",
        status="published",
        visibility="internal",
        size_bytes=10,
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        parse_error=None,
        embedding_status="failed",
        embedding_error="temporary",
        embedding_updated_at=None,
        logical_id="logical-1",
        version_number=1,
        is_current=True,
        supersedes_id=None,
    )

    class FakeStore:
        current = document

        def get_document(self, _document_id):
            return self.current

    store = FakeStore()

    class FakeAgent:
        def index_document(self, _document_id):
            store.current = replace(document, embedding_status="ready", embedding_error=None)
            return 4

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", lambda: store)
    monkeypatch.setattr(knowledge_router, "get_rag_agent", lambda: FakeAgent())
    response = TestClient(app).post("/knowledge/documents/doc-1/embedding/retry")
    assert response.status_code == 200
    assert response.json()["document"]["embedding_status"] == "ready"
    assert response.json()["indexed_chunks"] == 4


def test_reprocess_endpoint_rebuilds_chunks_and_embeddings(monkeypatch) -> None:
    document = Document(
        id="doc-1",
        title="制度",
        original_name="制度.pdf",
        category="制度",
        status="published",
        visibility="public",
        size_bytes=10,
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        parse_error=None,
        embedding_status="ready",
        embedding_error=None,
        embedding_updated_at=None,
        logical_id="logical-1",
        version_number=1,
        is_current=True,
        supersedes_id=None,
        parse_profile={"document_type": "native_text_pdf", "table_count": 1},
    )

    class FakeStore:
        current = document

        def reprocess_document(self, _document_id):
            self.current = replace(document, embedding_status="not_requested")
            return self.current, 7

        def get_document(self, _document_id):
            return self.current

    store = FakeStore()

    class FakeAgent:
        def index_document(self, _document_id):
            store.current = replace(store.current, embedding_status="ready")
            return 7

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", lambda: store)
    monkeypatch.setattr(knowledge_router, "get_rag_agent", lambda: FakeAgent())
    response = TestClient(app).post("/knowledge/documents/doc-1/reprocess")
    assert response.status_code == 200
    assert response.json()["parsed_chunks"] == 7
    assert response.json()["indexed_chunks"] == 7
    assert response.json()["document"]["embedding_status"] == "ready"


def test_explicit_detection_report_query_never_falls_back_to_policy_rag(monkeypatch) -> None:
    class EmptyReportStore:
        def match_detection_reports(self, *_args, **_kwargs):
            return []

    class Owner(KnowledgeToolMixin):
        _knowledge_choices = {}

        @staticmethod
        def _state_get(state, key, default=None):
            return state.get(key, default)

        @staticmethod
        def _state_pop(state, key):
            return state.pop(key, None)

    monkeypatch.setattr(
        "app.knowledge.service.get_knowledge_store", lambda: EmptyReportStore()
    )
    monkeypatch.setattr(
        "app.business.service.get_rag_agent",
        lambda: (_ for _ in ()).throw(AssertionError("不应回退到制度正文 RAG")),
    )
    decision = IntentDecision(
        Intent.GENERAL,
        0.97,
        "明确查询检测报告",
        "我们有检测报告吗",
    )

    reply = Owner()._answer_knowledge(
        "employee-1",
        "我们有检测报告吗",
        "我们有检测报告吗",
        decision,
    )

    assert reply.sources == []
    assert "检测报告" in reply.content
    assert "资料标题或文件名" in reply.content
