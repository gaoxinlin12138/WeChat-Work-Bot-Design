from pathlib import Path
from uuid import uuid4

from app.knowledge.service import get_knowledge_store


def main() -> None:
    store = get_knowledge_store()
    marker = f"知识库验收标记{uuid4().hex}"
    document = store.ingest(
        title="自动验收资料",
        original_name="smoke-test.txt",
        category="测试",
        content=f"{marker}，材质为不锈钢。".encode(),
    )
    try:
        assert document.status == "pending_review"
        assert store.search(marker) == []
        store.publish(document.id)
        results = store.search(marker)
        assert results and results[0].document_id == document.id
        print("知识库闭环验收成功：上传、待复核、发布、检索均正常。")
    finally:
        with store._connect() as connection:  # 仅用于删除自动验收数据
            row = connection.execute(
                "SELECT stored_path FROM knowledge_documents WHERE id = %s", (document.id,)
            ).fetchone()
            connection.execute("DELETE FROM knowledge_documents WHERE id = %s", (document.id,))
        if row:
            Path(row["stored_path"]).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
