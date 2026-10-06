import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.knowledge.processor import chunk_pages, extract_pages, summarize_extraction
from app.rerank import rerank_matches
from app.retrieval import hybrid_rerank


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    original_name: str
    category: str
    status: str
    visibility: str
    size_bytes: int
    created_at: datetime
    published_at: datetime | None
    parse_error: str | None
    embedding_status: str
    embedding_error: str | None
    embedding_updated_at: datetime | None
    logical_id: str
    version_number: int
    is_current: bool
    supersedes_id: str | None
    deleted_at: datetime | None = None
    deleted_from_status: str | None = None
    parse_profile: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None


@dataclass(frozen=True)
class SearchResult:
    document_id: str
    title: str
    category: str
    content: str
    page_number: int | None
    score: float
    chunk_index: int | None = None
    printed_page: str | None = None
    element_type: str = "text"
    section_path: tuple[str, ...] = ()
    clause_numbers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChunkForEmbedding:
    id: int
    content: str


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class DuplicateDocumentError(ValueError):
    pass


class DocumentNotFoundError(ValueError):
    pass


class DocumentNotReadyError(ValueError):
    pass


def psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class KnowledgeStore:
    def __init__(
        self, database_url: str, upload_dir: str, embedding_dimensions: int = 1536
    ) -> None:
        if not 1 <= embedding_dimensions <= 2000:
            raise ValueError("HNSW vector 维度必须在 1 到 2000 之间")
        self.database_url = psycopg_url(database_url)
        self.upload_dir = Path(upload_dir)
        self.embedding_dimensions = embedding_dimensions
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        self._initialization_lock = Lock()

    def _connect(self) -> psycopg.Connection:
        connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        register_vector(connection)
        return connection

    def initialize(self) -> None:
        with self._initialization_lock:
            if self._initialized:
                return
            self._initialize()

    def _initialize(self) -> None:
        dimensions = self.embedding_dimensions
        statements = (
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id uuid PRIMARY KEY,
                title varchar(300) NOT NULL,
                original_name varchar(500) NOT NULL,
                extension varchar(20) NOT NULL,
                category varchar(100) NOT NULL,
                status varchar(30) NOT NULL CHECK (
                    status IN ('pending_review', 'published', 'needs_ocr', 'needs_attention',
                               'disabled', 'deleted')
                ),
                visibility varchar(30) NOT NULL DEFAULT 'public',
                content_hash char(64) NOT NULL UNIQUE,
                size_bytes bigint NOT NULL,
                stored_path text NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                published_at timestamptz,
                parse_error text,
                embedding_status varchar(30) NOT NULL DEFAULT 'not_requested',
                embedding_error text,
                embedding_updated_at timestamptz,
                logical_id uuid NOT NULL,
                version_number integer NOT NULL DEFAULT 1,
                is_current boolean NOT NULL DEFAULT true,
                supersedes_id uuid REFERENCES knowledge_documents(id),
                deleted_at timestamptz,
                deleted_from_status varchar(30),
                parse_profile jsonb NOT NULL DEFAULT '{}'::jsonb
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id bigserial PRIMARY KEY,
                document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                chunk_index integer NOT NULL,
                content text NOT NULL,
                page_number integer,
                printed_page varchar(50),
                element_type varchar(30) NOT NULL DEFAULT 'text',
                section_path text[] NOT NULL DEFAULT '{{}}',
                clause_numbers text[] NOT NULL DEFAULT '{{}}',
                metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                embedding vector({dimensions}),
                embedding_model varchar(200),
                UNIQUE(document_id, chunk_index)
            )
            """,
            f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({dimensions})",
            """ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS
            embedding_status varchar(30) NOT NULL DEFAULT 'not_requested'""",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS embedding_error text",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
            "embedding_updated_at timestamptz",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
            "updated_at timestamptz NOT NULL DEFAULT now()",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS logical_id uuid",
            """ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS
            version_number integer NOT NULL DEFAULT 1""",
            """ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS
            is_current boolean NOT NULL DEFAULT true""",
            """ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS
            supersedes_id uuid REFERENCES knowledge_documents(id)""",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS deleted_at timestamptz",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
            "deleted_from_status varchar(30)",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
            "parse_profile jsonb NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
            "metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS "
            "knowledge_documents_status_check",
            "ALTER TABLE knowledge_documents ADD CONSTRAINT "
            "knowledge_documents_status_check CHECK (status IN "
            "('pending_review','published','needs_ocr','needs_attention',"
            "'disabled','deleted'))",
            "UPDATE knowledge_documents SET logical_id=id WHERE logical_id IS NULL",
            "ALTER TABLE knowledge_documents ALTER COLUMN logical_id SET NOT NULL",
            """ALTER TABLE knowledge_chunks
            ADD COLUMN IF NOT EXISTS embedding_model varchar(200)""",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS printed_page varchar(50)",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "element_type varchar(30) NOT NULL DEFAULT 'text'",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "section_path text[] NOT NULL DEFAULT '{}'",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "clause_numbers text[] NOT NULL DEFAULT '{}'",
            "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
            """
            CREATE TABLE IF NOT EXISTS agent_messages (
                id bigserial PRIMARY KEY,
                user_id varchar(200) NOT NULL,
                role varchar(20) NOT NULL CHECK (role IN ('user', 'assistant')),
                content text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'knowledge_chunks'
                      AND column_name = 'embedding'
                      AND udt_name = '_float8'
                ) THEN
                    ALTER TABLE knowledge_chunks
                    ALTER COLUMN embedding TYPE vector({dimensions})
                    USING embedding::vector({dimensions});
                END IF;
            END $$
            """,
            """
            CREATE INDEX IF NOT EXISTS knowledge_chunks_content_trgm_idx
            ON knowledge_chunks USING gin (content gin_trgm_ops)
            """,
            """
            CREATE INDEX IF NOT EXISTS knowledge_documents_status_idx
            ON knowledge_documents (status, visibility)
            """,
            """CREATE INDEX IF NOT EXISTS knowledge_documents_versions_idx
            ON knowledge_documents (logical_id, version_number DESC)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_version_unique_idx
            ON knowledge_documents (logical_id, version_number)""",
            """CREATE TABLE IF NOT EXISTS knowledge_document_version_counters (
                logical_id uuid PRIMARY KEY,
                last_version integer NOT NULL
            )""",
            """INSERT INTO knowledge_document_version_counters (logical_id, last_version)
            SELECT logical_id, MAX(version_number) FROM knowledge_documents GROUP BY logical_id
            ON CONFLICT (logical_id) DO UPDATE SET last_version=GREATEST(
                knowledge_document_version_counters.last_version, EXCLUDED.last_version)""",
            """
            CREATE TABLE IF NOT EXISTS knowledge_lifecycle_audit_logs (
                id uuid PRIMARY KEY,
                logical_id uuid NOT NULL,
                document_id uuid NOT NULL,
                document_title varchar(300) NOT NULL,
                action varchar(30) NOT NULL,
                actor varchar(100) NOT NULL DEFAULT 'admin',
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            """CREATE INDEX IF NOT EXISTS knowledge_lifecycle_audit_created_idx
            ON knowledge_lifecycle_audit_logs (created_at DESC)""",
            """
            CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw_idx
            ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
            WHERE embedding IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS agent_messages_user_time_idx
            ON agent_messages (user_id, created_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS wecom_message_dedup (
                message_id varchar(300) PRIMARY KEY,
                received_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            """
            DELETE FROM wecom_message_dedup
            WHERE received_at < now() - interval '30 days'
            """,
        )
        with psycopg.connect(
            self.database_url, row_factory=dict_row, connect_timeout=5
        ) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() "
                "|| ':knowledge-initialize', 0))"
            )
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(connection)
            for statement in statements:
                connection.execute(statement)
        self._initialized = True

    def ingest(
        self,
        *,
        title: str,
        original_name: str,
        category: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        return self._ingest(
            title=title, original_name=original_name, category=category, content=content,
            metadata=metadata,
        )

    def _ingest(
        self,
        *,
        title: str,
        original_name: str,
        category: str,
        content: bytes,
        previous_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """保存原文件；检测报告不读取正文，其余制度保持兼容解析。"""
        digest = hashlib.sha256(content).hexdigest()
        extension = Path(original_name).suffix.lower()
        document_id = str(uuid4())
        storage_root = (
            self.upload_dir / "detection_reports"
            if category.strip() == "检测报告"
            else self.upload_dir
        )
        storage_root.mkdir(parents=True, exist_ok=True)
        stored_path = storage_root / f"{document_id}{extension}"
        created_at = datetime.now(UTC)

        with self._connect() as connection:
            duplicate = connection.execute(
                "SELECT id,status FROM knowledge_documents WHERE content_hash = %s", (digest,)
            ).fetchone()
        if duplicate:
            if duplicate["status"] == "deleted":
                raise DuplicateDocumentError(
                    f"相同文件已在回收站，资料ID：{duplicate['id']}；请先从回收站恢复"
                )
            raise DuplicateDocumentError(f"相同文件已经上传，资料ID：{duplicate['id']}")

        stored_path.write_bytes(content)
        status = "pending_review"
        parse_error: str | None = None
        metadata = dict(metadata or {})
        metadata.setdefault("document_type", category.strip())
        metadata.setdefault("file_format", extension.lstrip("."))
        if category.strip() == "检测报告":
            # 检测报告是可交付原文件，只按标题或原文件名匹配。
            chunks = []
            parse_profile = {"document_type": "detection_report_pdf", "storage_only": True}
        else:
            parse_profile = {}
            try:
                pages = extract_pages(stored_path)
                parse_profile = (
                    summarize_extraction(pages)
                    if extension == ".pdf"
                    else {"document_type": "text_document", "page_count": 1}
                )
                chunks = chunk_pages(pages)
                needs_ocr_pages = parse_profile.get("needs_ocr_pages", [])
                low_quality_pages = parse_profile.get("low_quality_pages", [])
                if parse_profile.get("document_type") == "scanned_pdf" or (
                    not chunks and extension == ".pdf"
                ):
                    status = "needs_ocr"
                    parse_error = "PDF 未提取到文字，可能是图片型 PDF，需要 OCR"
                elif needs_ocr_pages:
                    status = "needs_attention"
                    parse_error = f"PDF 第 {needs_ocr_pages} 页需要 OCR 或人工复核"
                elif low_quality_pages:
                    status = "needs_attention"
                    parse_error = f"PDF 第 {low_quality_pages} 页解析质量较低，需要人工复核"
                elif not chunks:
                    status = "needs_attention"
                    parse_error = "文件未提取到有效文字"
            except Exception as exc:
                chunks = []
                status = "needs_attention"
                parse_error = str(exc)
                parse_profile = {"document_type": "parse_failed", "error_type": type(exc).__name__}

        try:
            with self._connect() as connection:
                logical_id = document_id
                version_number = 1
                if previous_id is not None:
                    previous = self._lock_document(connection, previous_id)
                    if previous is None:
                        raise DocumentNotFoundError("被替换的资料不存在")
                    if previous.status == "deleted":
                        raise DocumentNotReadyError("回收站资料请先恢复，再上传替换文件")
                    logical_id = previous.logical_id
                    title = title or previous.title
                    category = category or previous.category
                    if not metadata:
                        metadata = dict(previous.metadata)
                # 相同文件的并发上传共用事务锁；失败时清理本次文件，
                # 不会留下未关联的“独立制度”或原始唯一索引错误。
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"knowledge-content:{digest}",),
                )
                duplicate = connection.execute(
                    "SELECT id FROM knowledge_documents WHERE content_hash=%s", (digest,)
                ).fetchone()
                if duplicate:
                    raise DuplicateDocumentError("相同文件已经上传，请使用已有资料")
                version = connection.execute(
                    """INSERT INTO knowledge_document_version_counters
                    (logical_id,last_version)
                    SELECT %s,COALESCE(MAX(version_number),0)+1
                    FROM knowledge_documents WHERE logical_id=%s
                    ON CONFLICT (logical_id) DO UPDATE SET last_version=GREATEST(
                        knowledge_document_version_counters.last_version+1, EXCLUDED.last_version)
                    RETURNING last_version""",
                    (logical_id, logical_id),
                ).fetchone()
                version_number = version["last_version"]
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        id, title, original_name, extension, category, status, visibility,
                        content_hash, size_bytes, stored_path, created_at, parse_error,
                        logical_id, version_number, supersedes_id, is_current,
                        parse_profile, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'public', %s, %s, %s, %s, %s,
                              %s, %s, %s, false, %s, %s)
                    """,
                    (
                        document_id,
                        title,
                        original_name,
                        extension,
                        category,
                        status,
                        digest,
                        len(content),
                        str(stored_path),
                        created_at,
                        parse_error,
                        logical_id,
                        version_number,
                        previous_id,
                        Jsonb(parse_profile),
                        Jsonb(metadata),
                    ),
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO knowledge_chunks
                            (document_id, chunk_index, content, page_number, printed_page,
                             element_type, section_path, clause_numbers, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                document_id,
                                index,
                                chunk.content,
                                chunk.page_number,
                                getattr(chunk, "printed_page", None),
                                getattr(chunk, "element_type", "text"),
                                list(getattr(chunk, "section_path", ())),
                                list(getattr(chunk, "clause_numbers", ())),
                                Jsonb(getattr(chunk, "metadata", {})),
                            )
                            for index, chunk in enumerate(chunks)
                        ],
                    )
        except psycopg.errors.UniqueViolation as exc:
            stored_path.unlink(missing_ok=True)
            if exc.diag.constraint_name == "knowledge_documents_content_hash_key":
                raise DuplicateDocumentError("相同文件已经上传，请使用已有资料") from exc
            raise
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        return self.get_document(document_id)

    def get_document(self, document_id: str) -> Document:
        with self._connect() as connection:
            document = self._get_document(connection, document_id)
        if document is None:
            raise DocumentNotFoundError("资料不存在")
        return document

    @staticmethod
    def _get_document(connection: psycopg.Connection, document_id: str) -> Document | None:
        row = connection.execute(
            """
                SELECT id::text, title, original_name, category, status, visibility, content_hash,
                       size_bytes,
                       created_at, published_at, parse_error, embedding_status,
                       embedding_error, embedding_updated_at, logical_id::text,
                       version_number, is_current, supersedes_id::text,
                       deleted_at, deleted_from_status, parse_profile, metadata
                FROM knowledge_documents WHERE id = %s
                """,
            (document_id,),
        ).fetchone()
        return Document(**row) if row else None

    def _lock_document(self, connection: psycopg.Connection, document_id: str) -> Document | None:
        document = self._get_document(connection, document_id)
        if document is None:
            return None
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"knowledge-family:{document.logical_id}",),
        )
        # 等待锁期间目标可能已被永久删除、恢复或发布，必须重新读取。
        return self._get_document(connection, document_id)

    def list_documents(self, include_deleted: bool = False) -> list[Document]:
        # 兼容直接实例化 KnowledgeStore 的调用方：首次读取时补齐旧库迁移。
        # 正常服务启动已经会提前 initialize()，这里不会重复执行迁移。
        if not self._initialized:
            self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id::text, title, original_name, category, status, visibility, content_hash,
                       size_bytes,
                       created_at, published_at, parse_error, embedding_status,
                       embedding_error, embedding_updated_at, logical_id::text,
                       version_number, is_current, supersedes_id::text,
                       deleted_at, deleted_from_status, parse_profile, metadata
                FROM knowledge_documents
                WHERE %s OR status <> 'deleted'
                ORDER BY created_at DESC, version_number DESC
                """,
                (include_deleted,),
            ).fetchall()
        return [Document(**row) for row in rows]

    def publish(self, document_id: str) -> Document:
        with self._connect() as connection:
            document = self._lock_document(connection, document_id)
            if document is None:
                raise DocumentNotFoundError("资料不存在")
            latest = connection.execute(
                """SELECT MAX(version_number) AS version_number FROM knowledge_documents
                WHERE logical_id=%s AND status<>'deleted'""",
                (document.logical_id,),
            ).fetchone()
            if (
                document.status != "pending_review"
                or document.version_number != latest["version_number"]
            ):
                raise DocumentNotReadyError("只有解析完成且属于最新未删除版本的待复核资料才能发布")
            # 新版本发布后，旧版本进入回收站；文件、切片和向量保留，
            # 只有当前发布版本参与检索。
            connection.execute(
                """UPDATE knowledge_documents
                SET status='deleted', deleted_from_status=status,
                    deleted_at=now(), is_current=false, updated_at=now()
                WHERE logical_id=%s AND id<>%s AND status<>'deleted'""",
                (document.logical_id, document_id),
            )
            connection.execute(
                """UPDATE knowledge_documents SET status='published', published_at=now(),
                is_current=true, deleted_at=NULL, deleted_from_status=NULL,
                updated_at=now() WHERE id=%s""",
                (document_id,),
            )
        return self.get_document(document_id)

    def prepare_replacement(
        self, previous_id: str, *, title: str, original_name: str, category: str, content: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        if not category or not metadata:
            previous = self.get_document(previous_id)
            category = category or previous.category
            metadata = metadata or previous.metadata
        return self._ingest(
            title=title,
            original_name=original_name,
            category=category,
            content=content,
            previous_id=previous_id,
            metadata=metadata,
        )

    def delete_document(self, document_id: str, actor: str = "admin") -> bool:
        """仅将所选版本移入回收站，其他当前版和待复核版保持不变。"""
        with self._connect() as connection:
            document = self._lock_document(connection, document_id)
            if document is None or document.status == "deleted":
                return False
            logical_id = document.logical_id
            result = connection.execute(
                """UPDATE knowledge_documents SET
                    deleted_from_status=CASE WHEN status='deleted'
                        THEN deleted_from_status ELSE status END,
                    status='deleted', is_current=false, deleted_at=now(), updated_at=now()
                WHERE id=%s AND status <> 'deleted'""",
                (document_id,),
            )
            connection.execute(
                """INSERT INTO knowledge_lifecycle_audit_logs
                (id,logical_id,document_id,document_title,action,actor)
                VALUES (%s,%s,%s,%s,'trash',%s)""",
                (str(uuid4()), logical_id, document_id, document.title, actor),
            )
        return result.rowcount > 0

    def delete_documents(self, document_ids: list[str], actor: str = "admin") -> int:
        """在同一事务中将多个所选版本移入回收站。"""
        unique_ids = list(dict.fromkeys(document_ids))
        if not unique_ids:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id::text,logical_id::text,title FROM knowledge_documents
                WHERE id=ANY(%s::uuid[]) AND status<>'deleted'
                FOR UPDATE""",
                (unique_ids,),
            ).fetchall()
            if not rows:
                return 0
            selected_ids = [row["id"] for row in rows]
            connection.execute(
                """UPDATE knowledge_documents SET
                    deleted_from_status=status,status='deleted',is_current=false,
                    deleted_at=now(),updated_at=now()
                WHERE id=ANY(%s::uuid[]) AND status<>'deleted'""",
                (selected_ids,),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO knowledge_lifecycle_audit_logs
                    (id,logical_id,document_id,document_title,action,actor)
                    VALUES (%s,%s,%s,%s,'trash',%s)""",
                    [
                        (str(uuid4()), row["logical_id"], row["id"], row["title"], actor)
                        for row in rows
                    ],
                )
        return len(rows)

    def restore_document(self, document_id: str, actor: str = "admin") -> Document | None:
        """只恢复所选版本；恢复已发布历史时，将当前发布版移入回收站。"""
        with self._connect() as connection:
            document = self._lock_document(connection, document_id)
            if document is None or document.status != "deleted":
                return None
            logical_id = document.logical_id
            # 回收站中的历史发布版本可作为回退目标：先把当前版本移入回收站，
            # 再让选中的版本重新生效，避免出现两个 published/current 版本。
            if document.deleted_from_status == "published":
                connection.execute(
                    """UPDATE knowledge_documents SET status='deleted',
                    deleted_from_status=status, deleted_at=now(), is_current=false,
                    updated_at=now()
                    WHERE logical_id=%s AND status='published'""",
                    (logical_id,),
                )
                connection.execute(
                    """UPDATE knowledge_documents SET status='published',
                    is_current=true, published_at=COALESCE(published_at, now()),
                    deleted_from_status=NULL, deleted_at=NULL, updated_at=now()
                    WHERE id=%s""",
                    (document_id,),
                )
            else:
                connection.execute(
                    """UPDATE knowledge_documents SET
                        status=COALESCE(NULLIF(deleted_from_status,''),'disabled'),
                        is_current=false,
                        deleted_from_status=NULL, deleted_at=NULL, updated_at=now()
                    WHERE id=%s AND status='deleted'""",
                    (document_id,),
                )
            connection.execute(
                """INSERT INTO knowledge_lifecycle_audit_logs
                (id,logical_id,document_id,document_title,action,actor)
                VALUES (%s,%s,%s,%s,'restore',%s)""",
                (str(uuid4()), logical_id, document_id, document.title, actor),
            )
        return self.get_document(document_id)

    def permanently_delete_document(
        self, document_id: str, actor: str = "admin"
    ) -> list[str] | None:
        """永久删除所选回收站版本，返回它的文件路径。"""
        return self._permanently_delete_document(document_id, actor=actor)

    def _permanently_delete_document(
        self, document_id: str, *, actor: str, expired_before: datetime | None = None
    ) -> list[str] | None:
        with self._connect() as connection:
            document = self._lock_document(connection, document_id)
            if document is None or document.status != "deleted":
                return None
            if expired_before is not None and (
                document.deleted_at is None or document.deleted_at >= expired_before
            ):
                # 清理扫描后可能被恢复再入回收站，不能沿用过期的扫描结果。
                return None
            rows = connection.execute(
                """SELECT id::text,stored_path FROM knowledge_documents
                WHERE id=%s""",
                (document_id,),
            ).fetchall()
            connection.execute(
                """INSERT INTO knowledge_lifecycle_audit_logs
                (id,logical_id,document_id,document_title,action,actor)
                VALUES (%s,%s,%s,%s,'permanent_delete',%s)""",
                (str(uuid4()), document.logical_id, document_id, document.title, actor),
            )
            connection.execute(
                "UPDATE knowledge_documents SET supersedes_id=NULL WHERE supersedes_id=%s",
                (document_id,),
            )
            connection.execute(
                "DELETE FROM knowledge_documents WHERE id=%s AND status='deleted'",
                (document_id,),
            )
        return [str(row["stored_path"]) for row in rows]

    def purge_expired_documents(self, retention_days: int = 7) -> list[str]:
        """清理超过回收站保留期的制度文件。"""
        if retention_days < 1:
            raise ValueError("回收站保留期不能少于一天")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT d.id::text FROM knowledge_documents d
                WHERE d.status='deleted'
                  AND d.deleted_at < %s""",
                (cutoff,),
            ).fetchall()
        paths: list[str] = []
        for row in rows:
            deleted = self._permanently_delete_document(
                row["id"], actor="system", expired_before=cutoff
            )
            if deleted:
                paths.extend(deleted)
        return paths

    def trash_stale_drafts(self, retention_days: int = 7) -> int:
        """将长期未操作、且不参与检索的制度草稿移入回收站。"""
        if retention_days < 1:
            raise ValueError("草稿保留期不能少于一天")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connect() as connection:
            rows = connection.execute(
                """UPDATE knowledge_documents SET deleted_from_status=status,
                status='deleted',is_current=false,deleted_at=now(),updated_at=now()
                WHERE status IN ('pending_review','needs_ocr','needs_attention','disabled')
                  AND is_current=false AND updated_at < %s
                RETURNING id::text,logical_id::text,title""",
                (cutoff,),
            ).fetchall()
            if rows:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO knowledge_lifecycle_audit_logs
                        (id,logical_id,document_id,document_title,action,actor)
                        VALUES (%s,%s,%s,%s,'auto_trash','system')""",
                        [
                            (str(uuid4()), row["logical_id"], row["id"], row["title"])
                            for row in rows
                        ],
                    )
        return len(rows)

    def update_metadata(self, document_id: str, title: str, category: str) -> Document:
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE knowledge_documents SET title=%s, category=%s, updated_at=now()
                WHERE id=%s RETURNING id""",
                (title, category, document_id),
            ).fetchone()
        if not row:
            raise DocumentNotFoundError("资料不存在")
        return self.get_document(document_id)

    def document_chunks(
        self, document_id: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict[str, object]]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("预览页码或数量无效")
        self.get_document(document_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT chunk_index,page_number,printed_page,element_type,section_path,
                          clause_numbers,metadata,content FROM knowledge_chunks
                WHERE document_id=%s ORDER BY chunk_index LIMIT %s OFFSET %s""",
                (document_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def reprocess_document(self, document_id: str) -> tuple[Document, int]:
        """Reparse one stored file atomically while preserving its lifecycle state."""
        self.get_document(document_id)
        path = self.stored_path(document_id)
        pages = extract_pages(path)
        parse_profile = (
            summarize_extraction(pages)
            if path.suffix.lower() == ".pdf"
            else {"document_type": "text_document", "page_count": 1}
        )
        needs_review = [
            *parse_profile.get("needs_ocr_pages", []),
            *parse_profile.get("low_quality_pages", []),
        ]
        if needs_review:
            raise DocumentNotReadyError(
                f"第 {sorted(set(needs_review))} 页解析质量不足，已保留原索引"
            )
        chunks = chunk_pages(pages)
        if not chunks:
            raise DocumentNotReadyError("重新解析没有得到有效内容，已保留原索引")

        with self._connect() as connection:
            locked = self._lock_document(connection, document_id)
            if locked is None or locked.status == "deleted":
                raise DocumentNotFoundError("资料不存在或已在回收站")
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id=%s", (document_id,))
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO knowledge_chunks
                        (document_id, chunk_index, content, page_number, printed_page,
                         element_type, section_path, clause_numbers, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            document_id,
                            index,
                            chunk.content,
                            chunk.page_number,
                            getattr(chunk, "printed_page", None),
                            getattr(chunk, "element_type", "text"),
                            list(getattr(chunk, "section_path", ())),
                            list(getattr(chunk, "clause_numbers", ())),
                            Jsonb(getattr(chunk, "metadata", {})),
                        )
                        for index, chunk in enumerate(chunks)
                    ],
                )
            connection.execute(
                """UPDATE knowledge_documents
                SET parse_profile=%s, parse_error=NULL, embedding_status='not_requested',
                    embedding_error=NULL, updated_at=now()
                WHERE id=%s""",
                (Jsonb(parse_profile), document_id),
            )
        return self.get_document(document_id), len(chunks)

    def stored_path(self, document_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stored_path FROM knowledge_documents WHERE id=%s",
                (document_id,),
            ).fetchone()
        if not row:
            raise DocumentNotFoundError("资料不存在")
        return Path(row["stored_path"])

    def set_embedding_status(self, document_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_documents
                SET embedding_status = %s, embedding_error = %s,
                    embedding_updated_at = CASE WHEN %s = 'ready'
                        THEN now() ELSE embedding_updated_at END
                WHERE id = %s
                """,
                (status, error, status, document_id),
            )

    def claim_message(self, message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO wecom_message_dedup (message_id)
                VALUES (%s) ON CONFLICT DO NOTHING RETURNING message_id
                """,
                (message_id,),
            ).fetchone()
            return bool(row)

    def chunks_needing_embeddings(
        self, document_id: str, embedding_model: str
    ) -> list[ChunkForEmbedding]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, concat_ws(E'\n',
                    CASE WHEN cardinality(section_path)>0
                         THEN '章节：'||array_to_string(section_path, ' / ') END,
                    CASE WHEN element_type='table' THEN '内容类型：表格' END,
                    content) AS content
                FROM knowledge_chunks
                WHERE document_id = %s
                  AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s)
                ORDER BY chunk_index
                """,
                (document_id, embedding_model),
            ).fetchall()
        return [ChunkForEmbedding(**row) for row in rows]

    def save_embeddings(
        self, embeddings: list[tuple[int, list[float]]], embedding_model: str
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE knowledge_chunks SET embedding = %s, embedding_model = %s
                WHERE id = %s
                """,
                [(Vector(vector), embedding_model, chunk_id) for chunk_id, vector in embeddings],
            )

    def search(
        self,
        query: str,
        limit: int = 5,
        query_embedding: list[float] | None = None,
    ) -> list[SearchResult]:
        normalized = query.strip()
        if not normalized:
            return []
        with self._connect() as connection:
            corpus = connection.execute(
                """
                SELECT c.id::text AS result_key, c.document_id::text, d.title,
                       d.category, c.content, c.page_number, c.chunk_index,
                       c.printed_page, c.element_type, c.section_path,
                       c.clause_numbers, c.metadata
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.id = c.document_id
                WHERE d.status = 'published'
                  AND d.is_current = true
                  AND d.visibility = 'public'
                """
            ).fetchall()
            if not corpus:
                return []
            semantic_scores: dict[str, float] = {}
            if query_embedding:
                query_vector = Vector(query_embedding)
                semantic_rows = connection.execute(
                    """
                    SELECT c.id::text AS result_key,
                           (1 - (c.embedding <=> %s))::float AS semantic_score
                    FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id = c.document_id
                    WHERE d.status='published' AND d.is_current=true
                      AND d.visibility='public' AND c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> %s
                    LIMIT %s
                    """,
                    (query_vector, query_vector, max(limit * 4, 20)),
                ).fetchall()
                semantic_scores = {
                    row["result_key"]: float(row["semantic_score"]) for row in semantic_rows
                }
        documents = {
            row["result_key"]: "\n".join(
                part
                for part in (
                    row["title"],
                    " / ".join(row["section_path"] or []),
                    "表格" if row["element_type"] == "table" else "",
                    row["content"],
                )
                if part
            )
            for row in corpus
        }
        candidate_limit = max(limit * 4, 20)
        matches = hybrid_rerank(
            normalized,
            documents,
            semantic_scores,
            limit=candidate_limit,
            candidate_limit=candidate_limit,
        )
        matches = rerank_matches(normalized, documents, matches, limit=limit)
        rows_by_key = {row["result_key"]: row for row in corpus}
        return [
            SearchResult(
                document_id=rows_by_key[match.key]["document_id"],
                title=rows_by_key[match.key]["title"],
                category=rows_by_key[match.key]["category"],
                content=rows_by_key[match.key]["content"],
                page_number=rows_by_key[match.key]["page_number"],
                score=match.score,
                chunk_index=rows_by_key[match.key]["chunk_index"],
                printed_page=rows_by_key[match.key]["printed_page"],
                element_type=rows_by_key[match.key]["element_type"],
                section_path=tuple(rows_by_key[match.key]["section_path"] or ()),
                clause_numbers=tuple(rows_by_key[match.key]["clause_numbers"] or ()),
                metadata=dict(rows_by_key[match.key]["metadata"] or {}),
            )
            for match in matches
        ]

    @staticmethod
    def _report_search_values(document: Document) -> list[str]:
        return [
            " ".join(str(value).lower().split())
            for value in (document.title, document.original_name)
            if str(value).strip()
        ]

    def match_detection_reports(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[Document]:
        """按已发布检测报告的标题和原文件名匹配，不读取或索引正文。"""
        normalized = " ".join(query.lower().split())
        if not normalized:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id::text, title, original_name, category, status, visibility, content_hash,
                       size_bytes,
                       created_at, published_at, parse_error, embedding_status,
                       embedding_error, embedding_updated_at, logical_id::text,
                       version_number, is_current, supersedes_id::text,
                       deleted_at, deleted_from_status, parse_profile, metadata
                FROM knowledge_documents
                WHERE status='published' AND is_current=true AND category='检测报告'
                ORDER BY created_at DESC, version_number DESC
                LIMIT %s
                """,
                (max(limit * 5, 20),),
            ).fetchall()
        compact_query = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)
        report_query = "检测报告" in compact_query
        identifiers = set(re.findall(r"[a-z]*\d+[a-z0-9.-]*", normalized))
        descriptive_query = compact_query
        for phrase in (
            "请帮我",
            "帮我",
            "我们有",
            "有没有",
            "有无",
            "查询",
            "查找",
            "查一下",
            "发给我",
            "发送",
            "检测报告",
            "报告",
            "吗",
            "么",
            "呢",
        ):
            descriptive_query = descriptive_query.replace(phrase, "")
        scored: list[tuple[int, Document]] = []
        for row in rows:
            document = Document(**row)
            values = self._report_search_values(document)
            searchable = " ".join(values).lower()
            compact_searchable = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", searchable)
            score = 1 if report_query else 0
            score += sum(6 for identifier in identifiers if identifier in searchable)
            if descriptive_query and descriptive_query in compact_searchable:
                score += 5
            if compact_query and compact_query in compact_searchable:
                score += 4
            if score:
                scored.append((score, document))
        scored.sort(key=lambda item: (-item[0], -item[1].version_number, item[1].title))
        return [document for _, document in scored[:limit]]

    def recent_messages(self, user_id: str, limit: int = 6) -> list[ChatMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM agent_messages
                    WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s
                ) recent ORDER BY id
                """,
                (user_id, limit),
            ).fetchall()
        return [ChatMessage(**row) for row in rows]

    def add_message(self, user_id: str, role: str, content: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO agent_messages (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content),
            )

    @staticmethod
    def as_dict(document: Document) -> dict[str, object]:
        return asdict(document)
