from app.business.script_import import parse_scripts


def test_parse_markdown_scripts_with_metadata():
    data = """# 客户议价回复
【场景】报价议价
【适用条件】客户认为价格高
【必须信息】产品型号、数量、当前报价
【禁止表达】最低价、保证
【直接回复版】
您好，{产品名称}目前报价是{当前报价}，我按数量再帮您核一下。

# 售后投诉回复
【场景】售后投诉
【适用条件】客户反馈产品故障
【直接回复版】
麻烦发一下照片和订单信息，我帮您核实。
""".encode()
    scripts = parse_scripts("话术.md", data)
    assert len(scripts) == 2
    assert scripts[0]["scenario"] == "报价议价"
    assert scripts[0]["required_fields"] == ["产品型号", "数量", "当前报价"]
    assert scripts[1]["scenario"] == "售后投诉"


def test_upload_endpoint_creates_drafts(monkeypatch):
    from fastapi.testclient import TestClient

    import app.business.router as business_router
    from app.main import app

    class FakeStore:
        def __init__(self):
            self.items = []

        def create_script(self, **values):
            self.items.append(values)
            return values

    store = FakeStore()
    monkeypatch.setattr(business_router, "get_business_store", lambda: store)
    monkeypatch.setattr(
        business_router,
        "_classify_case",
        lambda question, answer: {
            "scenario": "报价议价",
            "intent": "报价议价",
            "emotion": "平和",
            "quality_score": 0.8,
        },
    )
    payload = "# 议价回复\n【场景】报价议价\n【直接回复版】\n您好，我帮您核价。".encode()
    response = TestClient(app).post(
        "/business/scripts/upload",
        files={"file": ("demo.md", payload, "text/markdown")},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert store.items[0]["scenario"] == "报价议价"


def test_edit_script_reclassifies_and_delegates_version_handling(monkeypatch):
    from fastapi.testclient import TestClient

    import app.business.router as business_router
    from app.main import app

    class FakeStore:
        def __init__(self):
            self.script_id = ""
            self.values = {}

        def save_script_edit(self, script_id, values):
            self.script_id = script_id
            self.values = values
            return {"id": script_id, "edit_result": "version_created", **values}

    store = FakeStore()
    monkeypatch.setattr(business_router, "get_business_store", lambda: store)
    monkeypatch.setattr(
        business_router,
        "_classify_case",
        lambda question, answer: {
            "scenario": "售后投诉",
            "intent": "反馈破损",
            "emotion": "不满",
            "quality_score": 0.9,
        },
    )

    response = TestClient(app).put(
        "/business/scripts/script-1",
        json={
            "customer_question": "  收到货破损了怎么办？  ",
            "employee_reply": "  请把订单号和破损照片发给我。  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["edit_result"] == "version_created"
    assert store.script_id == "script-1"
    assert store.values["customer_question"] == "收到货破损了怎么办？"
    assert store.values["content"] == "请把订单号和破损照片发给我。"
    assert store.values["scenario"] == "售后投诉"
    assert store.values["intent"] == "反馈破损"


def test_edit_script_returns_not_found(monkeypatch):
    from fastapi.testclient import TestClient

    import app.business.router as business_router
    from app.main import app

    class FakeStore:
        def save_script_edit(self, script_id, values):
            return None

    monkeypatch.setattr(business_router, "get_business_store", lambda: FakeStore())
    monkeypatch.setattr(
        business_router,
        "_classify_case",
        lambda question, answer: {
            "scenario": "未分类",
            "intent": "未分类",
            "emotion": "平和",
            "quality_score": 0.8,
        },
    )

    response = TestClient(app).put(
        "/business/scripts/missing",
        json={"customer_question": "问题", "employee_reply": "回答"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "话术不存在"
