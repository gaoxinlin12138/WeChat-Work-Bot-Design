import asyncio
import threading
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

import app.business.router as routes
from app.business.script_import import MAX_IMPORT_CASES, parse_xlsx_cases
from app.main import app


def test_batch_limit_checked_before_classification_or_database(monkeypatch):
    called = []
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(business_script_max_upload_mb=1)
    )
    monkeypatch.setattr(routes, "_classify_case", lambda *args: called.append(args))
    payload = "\n".join(f"# 案例 {i}\n回答内容" for i in range(MAX_IMPORT_CASES + 1))
    response = TestClient(app).post(
        "/business/scripts/upload", files={"file": ("cases.md", payload.encode())}
    )
    assert response.status_code == 413
    assert not called


@pytest.mark.asyncio
async def test_batch_import_runs_off_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    threads = []
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(business_script_max_upload_mb=1)
    )

    def fake_import(*args):
        threads.append(threading.get_ident())
        return {"count": 1, "scripts": []}

    monkeypatch.setattr(routes, "_import_scripts", fake_import)
    result = await routes.upload_scripts(UploadFile(filename="case.txt", file=BytesIO(b"case")))
    await asyncio.sleep(0)
    assert result["count"] == 1
    assert threads and threads[0] != event_loop_thread


def test_xlsx_inline_strings_empty_columns_and_newlines_preserved():
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet
          xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
          <row r="1"><c r="A1" t="inlineStr"><is><t>客户当时询问什么</t></is></c>
          <c r="C1" t="inlineStr"><is><t>员工实际回复</t></is></c></row>
          <row r="2"><c r="A2" t="inlineStr"><is><t>什么时候发货？</t></is></c>
          <c r="C2" t="inlineStr"><is><t>第一行 | 第二段
第三段</t></is></c></row></sheetData></worksheet>""",
        )
    cases = parse_xlsx_cases(buffer.getvalue())
    assert len(cases) == 1
    assert cases[0]["customer_question"] == "什么时候发货？"
    assert cases[0]["employee_reply"] == "第一行 | 第二段\n第三段"


def test_admin_query_session_is_namespaced(monkeypatch):
    calls = []

    class Agent:
        def answer(self, user_id, query, **_kwargs):
            calls.append((user_id, query))
            return SimpleNamespace(
                intent="conversation",
                intent_confidence=1,
                intent_reason="",
                intent_source="test",
                understood_query=query,
                content="答复",
                sources=[],
            )

    monkeypatch.setattr(routes, "get_business_agent", Agent)
    client = TestClient(app)
    response = client.get("/business/query", params={"q": " 价格呢 ", "user_id": "employee"})
    assert response.status_code == 200
    assert calls == [("admin-test:employee", "价格呢")]
    assert client.get("/business/query", params={"q": "   "}).status_code == 422
