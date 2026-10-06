"""Bounded audit reproductions; synthetic records in a disposable schema only.

Run from the project root with .venv/Scripts/python.exe. Never initializes the
production store or modifies existing business rows. No external messages sent.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.business.intent import Intent, MediaAction
from app.business.service import BusinessAgent, RoutedReply
from app.business.store import BusinessStore
from app.business.tools.product import ProductToolMixin
from app.config import get_settings
from app.knowledge.store import psycopg_url
from app.wecom import long_connection as wecom

results = {}


class FakeClient:
    def __init__(self, _options=None):
        self.handlers = {}
        self.replies = []

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn
        return register

    async def reply_stream(self, frame, stream_id, content, finish):
        self.replies.append((content, finish))


async def transport_cases():
    calls = []

    class Directory:
        async def can_view_product_cost(self, _user):
            raise httpx.ReadTimeout("synthetic network timeout")

    def answer(*args, **kwargs):
        calls.append(True)
        return RoutedReply(Intent.PRODUCT, "synthetic success", [])

    frame = {"body": {"msgid": "audit-fixture", "chattype": "single",
                      "from": {"userid": "synthetic-user"},
                      "text": {"content": "LTA-822套装价多少"}}}
    with patch.object(wecom, "WSClient", FakeClient), \
         patch.object(wecom, "get_wecom_directory", lambda: Directory()), \
         patch.object(wecom, "get_rag_agent", lambda: SimpleNamespace(
             store=SimpleNamespace(claim_message=lambda _: True))), \
         patch.object(wecom, "get_business_agent", lambda: SimpleNamespace(answer=answer)):
        client = wecom.create_client(SimpleNamespace(wecom_bot_id="fixture", wecom_bot_secret="fixture"))
        await client.handlers["message.text"](frame)
        results["directory_timeout_blocks_ordinary_query"] = {
            "answer_calls": len(calls), "replies": client.replies}

    async def failed_load(*args, **kwargs):
        raise ValueError("synthetic image exceeds size limit")

    client = FakeClient()
    reply = RoutedReply(Intent.PRODUCT_MEDIA, "已找到图片，正在发送。", [
        {"media_type": "image", "media_path": "synthetic.png"}],
        media_action=MediaAction.SEND)
    with patch.object(wecom, "load_media_bytes", failed_load):
        await wecom.reply_routed_result(client, frame, reply, stream_id="synthetic-stream")
    results["all_image_sends_fail_but_reply_claims_sending"] = client.replies


def database_cases():
    base = psycopg_url(get_settings().database_url)
    schema = "codex_audit_" + uuid4().hex
    tables = ["reply_scripts", "reply_script_search_documents", "product_records",
              "product_media_assets", "product_media_links", "product_search_documents",
              "business_lifecycle_audit_logs"]
    with psycopg.connect(base, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            # Empty structural copies, with no copied foreign keys or live data.
            # All these tables use UUID IDs, so no production sequences are used.
            for table in tables:
                connection.execute(sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
            store = BusinessStore(make_conninfo(base, options=f"-c search_path={schema},public"))
            first = store.create_script("Audit fixture", "产品咨询", "customer", "old answer")
            store.publish_script(str(first["id"]))
            second = store.create_script_version(str(first["id"]), {"content": "new answer"})
            store.publish_script(str(second["id"]))
            store.delete_script(str(second["id"]))
            store.restore_script(str(second["id"]))
            rows = store.list_reply_scripts()
            results["script_restore_reactivates_old_versions"] = [
                {"version": r["version_number"], "status": r["status"], "current": r["is_current"]}
                for r in rows]

            product = store.create_product_record({"model": "AUDIT-001", "set_price": 100,
                                                   "material": "铜", "spray_modes": "4"})
            product_id = product["id"]
            store.set_product_status(product_id, "active")
            store.set_product_embedding_status(product_id, store.build_product_search_text(product_id), "not_requested")
            store.update_product_record(product_id, {"model": "AUDIT-001", "set_price": 200,
                                                      "material": "铜", "spray_modes": "4"})
            results["save_draft_on_published_product_changes_live_data"] = {
                "status": store.get_product_record(product_id)["status"],
                "live_content": store.get_active_product(product_id).content,
                "search_content": store.search_products("AUDIT-001套装价")[0].content}
            owner = SimpleNamespace(store=store)
            owner._product_field_map = ProductToolMixin._product_field_map
            owner._media_summary = ProductToolMixin._media_summary
            owner._component_summary = lambda p, **kw: "配件明细：资料中未登记"
            reply = ProductToolMixin._format_product_detail(owner, store.get_active_product(product_id), [], "AUDIT-001是什么材质、几档")
            results["multiple_product_fields_only_first_answered"] = reply.content

            store.delete_product_record(product_id)
            with store._connect() as c:
                c.execute("UPDATE product_records SET deleted_at=now()-interval '40 days' WHERE id=%s", (product_id,))
            original_delete = store.permanently_delete_product_record
            def restore_and_retrash_before_purge(target_id, actor="admin"):
                store.restore_product_record(target_id)
                store.delete_product_record(target_id)
                return original_delete(target_id, actor=actor)
            with patch.object(store, "permanently_delete_product_record", restore_and_retrash_before_purge):
                store.purge_expired_product_records(7)
            results["newly_retrashed_product_is_purged_using_stale_cutoff"] = store.get_product_record(product_id) is None

            product = store.create_product_record({"model": "AUDIT-002"})
            product_id = product["id"]
            store.set_product_status(product_id, "active")
            store.delete_product_record(product_id)
            connect = store._connect

            class InterleavedConnection:
                def __enter__(self):
                    self.c = connect()
                    self.c.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.c.__exit__(*args)

                def execute(self, statement, params=()):
                    if "SELECT a.id::text,a.storage_path" in statement:
                        with patch.object(store, "_connect", connect):
                            store.restore_product_record(product_id)
                            results["restored_before_delete_status"] = store.get_product_record(product_id)["status"]
                    return self.c.execute(statement, params)

            with patch.object(store, "_connect", InterleavedConnection):
                store.permanently_delete_product_record(product_id)
            results["concurrently_restored_active_product_deleted"] = store.get_product_record(product_id) is None
        finally:
            assert schema.startswith("codex_audit_") and len(schema) == 44
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            results["temporary_schema_removed"] = True


if __name__ == "__main__":
    asyncio.run(transport_cases())
    database_cases()
    output = Path(__file__).with_name("reproduction-results.json")
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
