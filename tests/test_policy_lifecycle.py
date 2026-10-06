from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge import router as knowledge_router
from app.knowledge import store as store_module
from app.knowledge.file_cleanup import delete_document_files
from app.knowledge.store import (
    DocumentNotFoundError,
    DocumentNotReadyError,
    DuplicateDocumentError,
    KnowledgeStore,
)


@pytest.fixture
def policy_store(isolated_database_url, tmp_path):
    store = KnowledgeStore(isolated_database_url, str(tmp_path / "policies"), 3)
    store.initialize()
    return store


def upload(store, text="6.1 员工应按时打卡。"):
    return store.ingest(
        title="考勤制度", original_name="考勤制度.txt", category="人事", content=text.encode()
    )


def replace(store, previous_id, text="6.1 员工应按新制度打卡。"):
    return store.prepare_replacement(
        previous_id, title="", original_name="考勤制度.txt", category="", content=text.encode()
    )


def published_versions(store):
    return [d.id for d in store.list_documents() if d.status == "published" and d.is_current]


def test_batch_delete_moves_selected_documents_to_trash(policy_store):
    first = upload(policy_store, "6.1 第一份批量删除测试制度。")
    second = policy_store.ingest(
        title="第二份制度",
        original_name="第二份制度.txt",
        category="人事",
        content="6.2 第二份批量删除测试制度。".encode(),
    )

    assert policy_store.delete_documents([first.id, second.id, first.id]) == 2
    assert policy_store.get_document(first.id).status == "deleted"
    assert policy_store.get_document(second.id).status == "deleted"


def test_replace_waits_for_review_and_publish_preserves_history(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    chunks = store.chunks_needing_embeddings(original.id, "test-model")
    store.save_embeddings([(chunk.id, [1, 0, 0]) for chunk in chunks], "test-model")
    replacement = replace(store, original.id)
    assert replacement.status == "pending_review"
    assert replacement.version_number == 2
    assert replacement.supersedes_id == original.id
    assert not replacement.is_current
    assert {d.id for d in store.list_documents()} == {original.id, replacement.id}
    assert published_versions(store) == [original.id]

    store.publish(replacement.id)
    history = store.get_document(original.id)
    assert history.status == "deleted"
    assert history.deleted_from_status == "published"
    assert history.deleted_at is not None
    assert store.stored_path(original.id).is_file()
    assert store.document_chunks(original.id)
    assert store.chunks_needing_embeddings(original.id, "test-model") == []
    assert published_versions(store) == [replacement.id]


def test_rollback_is_visible_and_preserves_pending_replacement(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    second = store.publish(replace(store, original.id).id)
    pending = replace(store, second.id, "6.1 第三版需要复核。")

    restored = store.restore_document(original.id)
    assert restored.status == "published"
    assert restored.is_current
    assert store.get_document(second.id).status == "deleted"
    assert store.get_document(pending.id).status == "pending_review"
    assert published_versions(store) == [original.id]
    assert {d.id for d in store.list_documents()} == {original.id, pending.id}
    assert {d.id for d in store.list_documents(True)} == {original.id, second.id, pending.id}


def test_trashing_and_restoring_draft_affect_only_selected_version(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    first_draft = replace(store, original.id)
    second_draft = replace(store, original.id, "6.1 第三版草稿。")
    assert store.delete_document(first_draft.id)
    assert store.delete_document(second_draft.id)
    assert published_versions(store) == [original.id]

    restored = store.restore_document(first_draft.id)
    assert restored.status == "pending_review"
    assert not restored.is_current
    assert store.get_document(second_draft.id).status == "deleted"
    assert store.get_document(original.id).status == "published"
    # A higher version in the recycle bin must not block this review.
    assert store.publish(first_draft.id).status == "published"
    assert published_versions(store) == [first_draft.id]


def test_permanent_delete_clears_only_selected_version_and_its_fk(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    second = store.publish(replace(store, original.id).id)
    original_path = store.stored_path(original.id)
    second_path = store.stored_path(second.id)
    assert store.permanently_delete_document(second.id) is None

    paths = store.permanently_delete_document(original.id)
    assert paths == [str(original_path)]
    assert delete_document_files(paths, str(store.upload_dir)) == 1
    assert not original_path.exists()
    assert second_path.is_file()
    assert store.get_document(second.id).supersedes_id is None
    assert store.document_chunks(second.id)
    assert published_versions(store) == [second.id]
    with pytest.raises(DocumentNotFoundError):
        store.get_document(original.id)
    with store._connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS count FROM knowledge_chunks WHERE document_id=%s",
                (original.id,),
            ).fetchone()["count"]
            == 0
        )


def test_expiry_does_not_delete_live_or_recent_sibling_versions(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    second = store.publish(replace(store, original.id).id)
    third = store.publish(replace(store, second.id, "6.1 第三版制度。").id)
    original_path = store.stored_path(original.id)
    with store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_documents SET deleted_at=now()-interval '40 days' WHERE id=%s",
            (original.id,),
        )
    assert store.purge_expired_documents(30) == [str(original_path)]
    assert store.get_document(second.id).status == "deleted"
    assert store.get_document(third.id).status == "published"
    assert store.stored_path(third.id).is_file()
    assert published_versions(store) == [third.id]


def test_expiry_rechecks_after_restore_and_retrash(policy_store, monkeypatch):
    store = policy_store
    original = upload(store)
    store.delete_document(original.id)
    with store._connect() as connection:
        connection.execute(
            "UPDATE knowledge_documents SET deleted_at=now()-interval '40 days' WHERE id=%s",
            (original.id,),
        )
    delete = store._permanently_delete_document

    def restore_before_cleanup(document_id, *, actor, expired_before):
        store.restore_document(document_id)
        store.delete_document(document_id)
        return delete(document_id, actor=actor, expired_before=expired_before)

    monkeypatch.setattr(store, "_permanently_delete_document", restore_before_cleanup)
    assert store.purge_expired_documents(30) == []
    assert store.get_document(original.id).deleted_at > datetime.now(UTC) - timedelta(days=1)


def test_version_numbers_are_not_reused_after_permanent_deletion(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    second = replace(store, original.id)
    store.delete_document(second.id)
    store.permanently_delete_document(second.id)
    third = replace(store, original.id, "6.1 新上传的版本仍应是第三版。")
    assert third.version_number == 3


def test_replacement_error_cleans_file_and_leaves_no_orphan(policy_store):
    store = policy_store
    original = upload(store)
    files = set(store.upload_dir.iterdir())
    store.delete_document(original.id)
    with pytest.raises(DocumentNotReadyError):
        replace(store, original.id)
    with pytest.raises(DocumentNotFoundError):
        replace(store, str(uuid4()))
    assert set(store.upload_dir.iterdir()) == files
    assert [d.id for d in store.list_documents(True)] == [original.id]


@pytest.mark.parametrize("replacement", [False, True])
def test_simultaneous_identical_uploads_are_deduplicated(policy_store, monkeypatch, replacement):
    store = policy_store
    previous = store.publish(upload(store).id) if replacement else None
    extract = store_module.extract_pages
    barrier = Barrier(2)

    def synchronized_extract(path: Path):
        pages = extract(path)
        barrier.wait(timeout=10)
        return pages

    monkeypatch.setattr(store_module, "extract_pages", synchronized_extract)

    def submit():
        try:
            if previous:
                return replace(store, previous.id, "6.1 并发上传同一文件。")
            return upload(store, "6.1 并发上传同一文件。")
        except DuplicateDocumentError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert sum(item is not None for item in results) == 1
    assert len(store.list_documents(True)) == (2 if previous else 1)
    assert len(list(store.upload_dir.iterdir())) == (2 if previous else 1)


def test_simultaneous_distinct_replacements_get_distinct_versions(policy_store, monkeypatch):
    store = policy_store
    original = store.publish(upload(store).id)
    extract = store_module.extract_pages
    barrier = Barrier(2)

    def synchronized_extract(path):
        pages = extract(path)
        barrier.wait(timeout=10)
        return pages

    monkeypatch.setattr(store_module, "extract_pages", synchronized_extract)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(replace, store, original.id, f"6.1 并发文件 {index}。")
            for index in range(2)
        ]
        documents = [future.result(timeout=20) for future in futures]
    assert {d.version_number for d in documents} == {2, 3}
    assert {d.logical_id for d in store.list_documents(True)} == {original.logical_id}
    older, latest = sorted(documents, key=lambda d: d.version_number)
    with pytest.raises(DocumentNotReadyError):
        store.publish(older.id)
    store.publish(latest.id)
    assert published_versions(store) == [latest.id]


def test_concurrent_restore_and_publish_leave_one_current_version(policy_store):
    store = policy_store
    original = store.publish(upload(store).id)
    second = store.publish(replace(store, original.id).id)
    pending = replace(store, second.id, "6.1 并发发布测试。")
    barrier = Barrier(2)

    def publish():
        barrier.wait(timeout=10)
        return store.publish(pending.id)

    def restore():
        barrier.wait(timeout=10)
        return store.restore_document(original.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish), executor.submit(restore)]
        for future in futures:
            future.result(timeout=20)
    assert len(published_versions(store)) == 1


def test_chunk_preview_is_paginated_in_database(policy_store):
    store = policy_store
    original = upload(store)
    with store._connect() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO knowledge_chunks(document_id,chunk_index,content,page_number)
                VALUES (%s,%s,%s,%s)""",
                [(original.id, i, f"测试条款 {i}", i) for i in range(1, 125)],
            )
    assert len(store.document_chunks(original.id)) == 100
    page = store.document_chunks(original.id, offset=20, limit=20)
    assert [c["chunk_index"] for c in page] == list(range(20, 40))
    assert store.document_chunks(original.id, offset=125, limit=20) == []


def test_reprocess_document_replaces_chunks_and_resets_embeddings(policy_store):
    original = upload(policy_store, "第六章 考勤管理\n6.1 员工应按时打卡。")
    policy_store.publish(original.id)
    pending = policy_store.chunks_needing_embeddings(original.id, "test-model")
    policy_store.save_embeddings([(item.id, [1, 0, 0]) for item in pending], "test-model")
    policy_store.set_embedding_status(original.id, "ready")

    document, count = policy_store.reprocess_document(original.id)
    chunks = policy_store.document_chunks(original.id)

    assert document.status == "published"
    assert document.embedding_status == "not_requested"
    assert document.parse_profile["document_type"] == "text_document"
    assert count == len(chunks) == 1
    assert chunks[0]["section_path"] == ["第六章 考勤管理", "6.1 员工应按时打卡。"]
    assert chunks[0]["clause_numbers"] == ["6.1"]


def test_chunk_write_failure_rolls_back_replacement_and_file(policy_store, monkeypatch):
    store = policy_store
    original = store.publish(upload(store).id)
    files = set(store.upload_dir.iterdir())
    monkeypatch.setattr(
        store_module, "chunk_pages", lambda _: [SimpleNamespace(content=None, page_number=1)]
    )
    with pytest.raises(store_module.psycopg.errors.NotNullViolation):
        replace(store, original.id)
    assert [d.id for d in store.list_documents(True)] == [original.id]
    assert set(store.upload_dir.iterdir()) == files


def test_search_uses_only_current_version_after_rollback(policy_store, monkeypatch):
    store = policy_store
    original = store.publish(upload(store).id)
    latest = store.publish(replace(store, original.id).id)
    monkeypatch.setattr(store_module, "rerank_matches", lambda q, d, m, **kwargs: m)
    assert {r.document_id for r in store.search("员工打卡")} == {latest.id}
    store.restore_document(original.id)
    assert {r.document_id for r in store.search("员工打卡")} == {original.id}


def test_policy_settings_and_preview_api_validate_pagination(monkeypatch):
    app = FastAPI()
    app.include_router(knowledge_router.router)
    monkeypatch.setattr(
        knowledge_router,
        "get_settings",
        lambda: Settings(_env_file=None, business_trash_retention_days=90),
    )

    class FakeStore:
        def document_chunks(self, document_id, *, offset=0, limit=100):
            assert (document_id, offset, limit) == ("policy", 20, 20)
            return [{"chunk_index": 20, "content": "第二页条款", "page_number": 2}]

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", FakeStore)
    client = TestClient(app)
    assert client.get("/knowledge/settings").json() == {
        "trash_retention_days": 90,
        "draft_retention_days": 7,
    }
    response = client.get("/knowledge/documents/policy/chunks?offset=20&limit=20")
    assert response.status_code == 200
    assert response.json()[0]["chunk_index"] == 20
    assert client.get("/knowledge/documents/policy/chunks?offset=-1").status_code == 422
    assert client.get("/knowledge/documents/policy/chunks?limit=101").status_code == 422


def test_replacement_of_trashed_document_returns_conflict(monkeypatch):
    app = FastAPI()
    app.include_router(knowledge_router.router)
    monkeypatch.setattr(knowledge_router, "get_settings", lambda: Settings(_env_file=None))

    class FakeStore:
        def prepare_replacement(self, *args, **kwargs):
            raise DocumentNotReadyError("回收站资料请先恢复，再上传替换文件")

    monkeypatch.setattr(knowledge_router, "get_knowledge_store", FakeStore)
    response = TestClient(app).post(
        "/knowledge/documents/policy/replacement",
        files={"file": ("test.txt", "制度内容".encode(), "text/plain")},
    )
    assert response.status_code == 409
    assert "先恢复" in response.json()["detail"]
