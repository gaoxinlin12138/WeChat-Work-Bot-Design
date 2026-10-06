from app.knowledge.service import get_knowledge_store


def main() -> None:
    store = get_knowledge_store()
    with store._connect() as connection:  # 仅用于本地安装自检
        tables = connection.execute(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename IN ('knowledge_documents', 'knowledge_chunks')
            ORDER BY tablename
            """
        ).fetchall()
        vector_column = connection.execute(
            """
            SELECT format_type(atttypid, atttypmod)
            FROM pg_attribute
            WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'
            """
        ).fetchone()
        hnsw_index = connection.execute(
            """
            SELECT indisvalid FROM pg_index
            WHERE indexrelid = 'knowledge_chunks_embedding_hnsw_idx'::regclass
            """
        ).fetchone()
    names = [row["tablename"] for row in tables]
    print(f"知识库数据表已就绪：{', '.join(names)}")
    print(f"向量字段：{vector_column['format_type'] if vector_column else '未创建'}")
    valid_index = bool(hnsw_index and hnsw_index["indisvalid"])
    print(f"HNSW 索引：{'有效' if valid_index else '无效或未创建'}")


if __name__ == "__main__":
    main()
