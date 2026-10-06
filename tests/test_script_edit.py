from app.business.store import BusinessStore


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []

    @property
    def rowcount(self):
        return 1


class FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, params=()):
        self.statements.append((statement, params))
        return FakeResult(self.rows.pop(0))


EDIT_VALUES = {
    "customer_question": "修改后的问题",
    "employee_reply": "修改后的回答",
    "content": "修改后的回答",
    "scenario": "售后投诉",
    "intent": "反馈破损",
    "emotion": "不满",
    "quality_score": 0.9,
    "validation_warnings": [],
}


def test_save_script_edit_updates_draft_in_place(monkeypatch):
    source = {
        "id": "draft-1",
        "status": "draft",
        "version_number": 1,
        "logical_id": "logical-1",
    }
    updated = {**source, **EDIT_VALUES}
    connection = FakeConnection([source, updated])
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    result = store.save_script_edit("draft-1", EDIT_VALUES)

    assert result["id"] == "draft-1"
    assert result["edit_result"] == "draft_updated"
    assert any("UPDATE reply_scripts" in statement for statement, _ in connection.statements)
    assert not any(
        "INSERT INTO reply_scripts" in statement for statement, _ in connection.statements
    )


def test_save_script_edit_creates_version_for_published_script(monkeypatch):
    source = {
        "id": "published-1",
        "title": "客户问答案例",
        "scenario": "产品咨询",
        "audience": "customer",
        "content": "原回答",
        "visibility": "internal",
        "status": "published",
        "conditions": "",
        "required_fields": [],
        "forbidden_phrases": [],
        "variables": [],
        "product_scope": "",
        "source_row": None,
        "logical_id": "logical-1",
        "version_number": 1,
    }
    created = {
        **source,
        **EDIT_VALUES,
        "id": "draft-2",
        "status": "draft",
        "version_number": 2,
        "is_current": False,
        "supersedes_id": "published-1",
    }
    connection = FakeConnection([source, None, {"value": 2}, created])
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    result = store.save_script_edit("published-1", EDIT_VALUES)

    assert result["id"] == "draft-2"
    assert result["edit_result"] == "version_created"
    assert any("INSERT INTO reply_scripts" in statement for statement, _ in connection.statements)


def test_publish_script_moves_old_versions_to_trash_before_publishing(monkeypatch):
    published = {
        "id": "draft-2",
        "logical_id": "logical-1",
        "status": "published",
        "is_current": True,
    }
    connection = FakeConnection([{"logical_id": "logical-1"}, None, published])
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    result = store.publish_script("draft-2")

    assert result == published
    delete_statement, delete_params = connection.statements[1]
    assert "status='deleted'" in delete_statement
    assert "deleted_at=now()" in delete_statement
    assert delete_params == ("logical-1", "draft-2")
    assert (
        "UPDATE reply_scripts SET status='published',is_current=true"
        in connection.statements[2][0]
    )


def test_delete_script_moves_whole_logical_group_to_trash(monkeypatch):
    deleted = {
        "id": "published-1",
        "logical_id": "logical-1",
        "status": "deleted",
    }
    connection = FakeConnection([{"logical_id": "logical-1"}, deleted])
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    result = store.delete_script("published-1")

    assert result == deleted
    statement, params = connection.statements[1]
    assert "WHERE logical_id=%s" in statement
    assert "deleted_from_status=status" in statement
    assert params == ("logical-1",)


def test_bulk_delete_scripts_deduplicates_ids_and_counts_logical_groups(monkeypatch):
    connection = FakeConnection(
        [
            [{"logical_id": "logical-1"}, {"logical_id": "logical-2"}],
            None,
        ]
    )
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    assert store.delete_scripts(["script-1", "script-1", "script-2"]) == 2
    lookup_statement, lookup_params = connection.statements[0]
    assert "SELECT DISTINCT logical_id" in lookup_statement
    assert lookup_params == (["script-1", "script-2"],)
    update_statement, update_params = connection.statements[1]
    assert "logical_id=ANY" in update_statement
    assert "deleted_from_status=status" in update_statement
    assert update_params == (["logical-1", "logical-2"],)


def test_restore_script_restores_requested_version_when_group_is_deleted(monkeypatch):
    restored = {
        "id": "published-1",
        "logical_id": "logical-1",
        "status": "published",
    }
    connection = FakeConnection(
        [{"logical_id": "logical-1", "deleted_from_status": "published"}, None, restored]
    )
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    result = store.restore_script("published-1")

    assert result == restored
    statement, params = connection.statements[2]
    assert "deleted_from_status" in statement
    assert "WHERE id=%s" in statement
    assert params == ("published-1",)


def test_restore_published_history_swaps_current_version(monkeypatch):
    restored = {
        "id": "published-1",
        "logical_id": "logical-1",
        "status": "published",
    }
    connection = FakeConnection(
        [
            {"logical_id": "logical-1", "deleted_from_status": "published"},
            {"?column?": 1},
            None,
            restored,
        ]
    )
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    assert store.restore_script("published-1") == restored
    assert "status='deleted'" in connection.statements[2][0]
    assert "WHERE id=%s" in connection.statements[3][0]
    assert connection.statements[3][1] == ("published-1",)


def test_permanent_delete_script_removes_historical_version_when_group_is_live(monkeypatch):
    connection = FakeConnection(
        [{"id": "history-1", "logical_id": "logical-1"}, None, None]
    )
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    assert store.permanently_delete_script("history-1") is True
    lookup_statement, lookup_params = connection.statements[0]
    assert "id=%s OR logical_id=%s" in lookup_statement
    assert lookup_params == ("history-1", "history-1", "history-1")
    statement, params = connection.statements[1]
    assert "DELETE FROM reply_scripts" in statement
    assert "r.id=%s" in statement
    assert params == ("history-1", "logical-1", "logical-1", "logical-1")
    assert "business_lifecycle_audit_logs" in connection.statements[2][0]


def test_permanent_delete_script_accepts_logical_id_for_fully_deleted_group(monkeypatch):
    connection = FakeConnection(
        [{"id": "latest-2", "logical_id": "logical-1"}, None, None]
    )
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    assert store.permanently_delete_script("logical-1") is True
    lookup_statement, lookup_params = connection.statements[0]
    assert "logical_id=%s" in lookup_statement
    assert lookup_params == ("logical-1", "logical-1", "logical-1")
    delete_statement, delete_params = connection.statements[1]
    assert "NOT EXISTS" in delete_statement
    assert delete_params == ("latest-2", "logical-1", "logical-1", "logical-1")


def test_stale_script_drafts_move_to_trash(monkeypatch):
    connection = FakeConnection([None])
    store = BusinessStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: connection)

    assert store.trash_stale_reply_script_drafts(7) == 1
    statement, params = connection.statements[0]
    assert "status='draft'" in statement
    assert "status='deleted'" in statement
    assert "updated_at <" in statement
    assert params == (7,)
