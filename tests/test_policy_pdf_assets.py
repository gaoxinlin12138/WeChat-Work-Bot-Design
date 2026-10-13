from pathlib import Path

import pytest

from app.knowledge.processor import chunk_pages, extract_pages, summarize_extraction

UPLOADS = Path(__file__).resolve().parents[1] / "data" / "uploads"


def test_current_policy_pdfs_have_native_text_and_page_provenance() -> None:
    pdfs = sorted(UPLOADS.glob("*.pdf"))
    assert len(pdfs) == 3

    for path in pdfs:
        pages = extract_pages(path)
        profile = summarize_extraction(pages)
        chunks = chunk_pages(pages)
        assert profile["document_type"] == "native_text_pdf"
        assert profile["needs_ocr_pages"] == []
        assert profile["low_quality_pages"] == []
        assert chunks
        assert all(chunk.page_number for chunk in chunks)
        assert any(chunk.clause_numbers for chunk in chunks)


def test_attendance_pdf_extracts_business_tables_and_removes_repeated_header() -> None:
    attendance_candidates = [path for path in UPLOADS.glob("*.pdf") if "7b4c8a88" in path.name]
    if not attendance_candidates:
        pytest.skip(
            "缺少考勤制度固定 fixture（历史文件名包含 7b4c8a88）；现有上传文件未能通过"
            "内容指纹安全确认，避免用另一份制度误充测试数据"
        )
    attendance = attendance_candidates[0]
    pages = extract_pages(attendance)
    profile = summarize_extraction(pages)
    table_text = "\n".join(page.content for page in pages if page.element_type == "table")
    body_page_three = next(
        page.content
        for page in pages
        if page.page_number == 3 and page.element_type == "text"
    )

    assert profile["table_pages"] == [3, 4, 5]
    assert "岗位=财务" in table_text
    assert "出勤类型=8H/天" in table_text
    assert "累计旷工班次=1个≤N＜2个" in table_text
    assert "Wenzhou Yuyin Sanitary Ware" not in body_page_three
    assert body_page_three.startswith("1 目的：")
