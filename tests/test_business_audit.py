from fastapi.testclient import TestClient

import app.business.router as business_router
from app.business.audit import normalize_audit_text, text_fingerprint
from app.main import app


def test_audit_text_keeps_original_content_for_admin_review() -> None:
    value = "客户电话 13812345678，订单号：ORDER-202609120001，邮箱 a@example.com"
    normalized = normalize_audit_text(value)

    assert normalized == value
    assert normalize_audit_text("  第一行\n 第二行  ") == "第一行 第二行"
    assert normalize_audit_text("12345", 3) == "123"
    assert text_fingerprint(value) != value


class AuditStore:
    def __init__(self) -> None:
        self.item = {
            "id": "audit-1",
            "query_redacted": "公司的迟到规定",
            "intent": "policy_query",
            "confidence": 0.96,
            "data_source": "document",
            "source_count": 1,
            "response_ms": 120,
        }
        self.correction = None

    def list_intent_audits(self, **kwargs):
        return [self.item]

    def get_intent_audit(self, audit_id):
        return self.item if audit_id == "audit-1" else None

    def correct_intent_audit(self, audit_id, corrected_intent, reason, corrected_by):
        if audit_id != "audit-1":
            return None
        self.correction = (corrected_intent, reason, corrected_by)
        return {**self.item, "corrected_intent": corrected_intent, "correction_reason": reason}


def test_intent_audit_admin_endpoints(monkeypatch) -> None:
    store = AuditStore()
    monkeypatch.setattr(business_router, "get_business_store", lambda: store)
    client = TestClient(app)

    listed = client.get("/business/intent-audits")
    assert listed.status_code == 200
    assert listed.json()[0]["intent"] == "policy_query"

    corrected = client.post(
        "/business/intent-audits/audit-1/correction",
        json={
            "corrected_intent": "product_query",
            "reason": "实际是在查产品",
            "corrected_by": "admin",
        },
    )
    assert corrected.status_code == 200
    assert store.correction == ("product_query", "实际是在查产品", "admin")

    assert client.get("/business/intent-audits/missing").status_code == 404
