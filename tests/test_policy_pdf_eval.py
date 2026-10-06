from app.knowledge.store import SearchResult
from scripts.evaluate_policy_pdf import PolicyPdfCase, result_matches


def test_real_policy_case_requires_title_page_content_and_element_type() -> None:
    case = PolicyPdfCase(
        case_id="table",
        query="财务岗位休几天",
        expected_title_keyword="考勤管理制度",
        expected_pages=[3],
        expected_content_keywords=["财务", "1天/周"],
        expected_element_type="table",
    )
    result = SearchResult(
        document_id="doc",
        title="YY-HR-SD-05 考勤管理制度",
        category="制度",
        content="第1行：岗位=财务；月休天数=1 天/周",
        page_number=3,
        score=0.9,
        element_type="table",
    )

    assert result_matches(case, result)
    assert not result_matches(case, SearchResult(**{**result.__dict__, "page_number": 4}))
    assert not result_matches(case, SearchResult(**{**result.__dict__, "element_type": "text"}))
