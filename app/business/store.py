from dataclasses import asdict, dataclass
from threading import Lock
from uuid import uuid4

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.business.audit import normalize_audit_text, text_fingerprint
from app.business.product_store import ProductStoreMixin
from app.knowledge.store import psycopg_url
from app.rerank import rerank_matches
from app.retrieval import hybrid_rerank


@dataclass(frozen=True)
class BusinessResult:
    source_type: str
    source_id: str
    title: str
    content: str
    score: float
    media_type: str | None = None
    media_path: str | None = None
    media_role: str | None = None


class BusinessStore(ProductStoreMixin):
    def __init__(self, database_url: str, embedding_dimensions: int = 1024) -> None:
        self.database_url = psycopg_url(database_url)
        self.embedding_dimensions = embedding_dimensions
        self._init_lock = Lock()
        self._initialized = False

    def _connect(self) -> psycopg.Connection:
        connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        register_vector(connection)
        return connection

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            self._initialize()
            self._initialized = True

    def _initialize(self) -> None:
        dimensions = self.embedding_dimensions
        statements = (
            """CREATE TABLE IF NOT EXISTS reply_scripts (
                id uuid PRIMARY KEY, title varchar(300) NOT NULL,
                scenario varchar(200) NOT NULL, audience varchar(30) NOT NULL,
                content text NOT NULL, visibility varchar(30) NOT NULL DEFAULT 'internal',
                status varchar(30) NOT NULL DEFAULT 'draft',
                conditions text NOT NULL DEFAULT '',
                required_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
                forbidden_phrases jsonb NOT NULL DEFAULT '[]'::jsonb,
                variables jsonb NOT NULL DEFAULT '[]'::jsonb,
                product_scope text NOT NULL DEFAULT '',
                validation_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
                customer_question text NOT NULL DEFAULT '',
                employee_reply text NOT NULL DEFAULT '',
                intent varchar(100) NOT NULL DEFAULT '',
                emotion varchar(100) NOT NULL DEFAULT '',
                quality_score numeric(5,2),
                source_row integer,
                logical_id uuid,
                version_number integer NOT NULL DEFAULT 1,
                is_current boolean NOT NULL DEFAULT true,
                supersedes_id uuid REFERENCES reply_scripts(id) ON DELETE SET NULL,
                deleted_at timestamptz,
                deleted_from_status varchar(30),
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now())""",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS conditions "
            "text NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS required_fields "
            "jsonb NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS forbidden_phrases "
            "jsonb NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS variables "
            "jsonb NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS product_scope "
            "text NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS validation_warnings "
            "jsonb NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS customer_question "
            "text NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS employee_reply "
            "text NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS intent "
            "varchar(100) NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS emotion "
            "varchar(100) NOT NULL DEFAULT ''",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS quality_score numeric(5,2)",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS source_row integer",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS logical_id uuid",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS version_number "
            "integer NOT NULL DEFAULT 1",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS is_current "
            "boolean NOT NULL DEFAULT true",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS supersedes_id uuid",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS deleted_at timestamptz",
            "ALTER TABLE reply_scripts ADD COLUMN IF NOT EXISTS deleted_from_status varchar(30)",
            "UPDATE reply_scripts SET logical_id=id WHERE logical_id IS NULL",
            "ALTER TABLE reply_scripts ALTER COLUMN logical_id SET NOT NULL",
            """UPDATE reply_scripts r SET supersedes_id=NULL
            WHERE supersedes_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM reply_scripts previous WHERE previous.id=r.supersedes_id
            )""",
            """DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname='reply_scripts_supersedes_id_fkey'
                  AND conrelid='reply_scripts'::regclass
            ) THEN
                ALTER TABLE reply_scripts ADD CONSTRAINT reply_scripts_supersedes_id_fkey
                FOREIGN KEY (supersedes_id) REFERENCES reply_scripts(id) ON DELETE SET NULL;
            END IF;
            END $$""",
            """UPDATE reply_scripts r SET status='deleted',is_current=false,
            deleted_from_status=CASE WHEN status='inactive' THEN 'draft' ELSE status END,
            deleted_at=COALESCE(deleted_at,updated_at,now())
            WHERE logical_id IN (
                SELECT logical_id FROM reply_scripts WHERE status='inactive'
            )""",
            """WITH ranked AS (
                SELECT id,ROW_NUMBER() OVER (
                    PARTITION BY logical_id ORDER BY version_number DESC,updated_at DESC,id DESC
                ) AS position
                FROM reply_scripts WHERE status='published' AND is_current=true
            )
            UPDATE reply_scripts r SET status='deleted',is_current=false,
                deleted_at=COALESCE(deleted_at,now()),deleted_from_status='published'
            FROM ranked WHERE r.id=ranked.id AND ranked.position>1""",
            """CREATE UNIQUE INDEX IF NOT EXISTS reply_scripts_one_current_published_idx
            ON reply_scripts (logical_id) WHERE status='published' AND is_current=true""",
            """CREATE INDEX IF NOT EXISTS reply_scripts_search_idx
            ON reply_scripts USING gin
            ((title || ' ' || scenario || ' ' || content) gin_trgm_ops)""",
            f"""CREATE TABLE IF NOT EXISTS reply_script_search_documents (
                script_id uuid PRIMARY KEY REFERENCES reply_scripts(id) ON DELETE CASCADE,
                search_text text NOT NULL DEFAULT '',
                embedding vector({dimensions}),
                embedding_model varchar(200),
                embedding_status varchar(30) NOT NULL DEFAULT 'not_requested',
                embedding_error text,
                updated_at timestamptz NOT NULL DEFAULT now())""",
            """CREATE TABLE IF NOT EXISTS wecom_media_cache (
                content_hash char(64) NOT NULL,
                media_type varchar(20) NOT NULL,
                media_id text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (content_hash,media_type))""",
            """CREATE INDEX IF NOT EXISTS reply_script_search_text_idx
            ON reply_script_search_documents USING gin (search_text gin_trgm_ops)""",
            """CREATE INDEX IF NOT EXISTS wecom_media_cache_expires_idx
            ON wecom_media_cache (expires_at)""",
            "DELETE FROM wecom_media_cache WHERE expires_at <= now()",
            """CREATE TABLE IF NOT EXISTS intent_audit_logs (
                id uuid PRIMARY KEY,
                created_at timestamptz NOT NULL DEFAULT now(),
                request_fingerprint char(64),
                user_fingerprint char(64) NOT NULL,
                query_fingerprint char(64) NOT NULL,
                query_redacted varchar(500) NOT NULL DEFAULT '',
                intent varchar(100) NOT NULL,
                confidence numeric(5,4) NOT NULL DEFAULT 0,
                intent_source varchar(60) NOT NULL DEFAULT '',
                intent_reason varchar(300) NOT NULL DEFAULT '',
                understood_query_redacted varchar(500) NOT NULL DEFAULT '',
                data_source varchar(200) NOT NULL DEFAULT 'none',
                response_preview varchar(800) NOT NULL DEFAULT '',
                source_count integer NOT NULL DEFAULT 0,
                response_ms numeric(12,2) NOT NULL DEFAULT 0,
                planner_version varchar(60) NOT NULL DEFAULT '',
                query_plan_redacted jsonb NOT NULL DEFAULT '{}'::jsonb,
                validation_result jsonb NOT NULL DEFAULT '{}'::jsonb,
                execution_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
                shadow_diff jsonb NOT NULL DEFAULT '{}'::jsonb,
                corrected_intent varchar(100),
                correction_reason varchar(500),
                corrected_by varchar(100),
                corrected_at timestamptz
            )""",
            """ALTER TABLE intent_audit_logs
            ADD COLUMN IF NOT EXISTS request_fingerprint char(64)""",
            "ALTER TABLE intent_audit_logs ADD COLUMN IF NOT EXISTS "
            "planner_version varchar(60) NOT NULL DEFAULT ''",
            "ALTER TABLE intent_audit_logs ADD COLUMN IF NOT EXISTS "
            "query_plan_redacted jsonb NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE intent_audit_logs ADD COLUMN IF NOT EXISTS "
            "validation_result jsonb NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE intent_audit_logs ADD COLUMN IF NOT EXISTS "
            "execution_summary jsonb NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE intent_audit_logs ADD COLUMN IF NOT EXISTS "
            "shadow_diff jsonb NOT NULL DEFAULT '{}'::jsonb",
            """CREATE UNIQUE INDEX IF NOT EXISTS intent_audit_request_idx
            ON intent_audit_logs (request_fingerprint)
            WHERE request_fingerprint IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS business_lifecycle_audit_logs (
                id uuid PRIMARY KEY,
                resource_type varchar(40) NOT NULL,
                resource_id uuid NOT NULL,
                action varchar(30) NOT NULL,
                actor varchar(100) NOT NULL DEFAULT 'admin',
                created_at timestamptz NOT NULL DEFAULT now()
            )""",
            """CREATE INDEX IF NOT EXISTS business_lifecycle_audit_created_idx
            ON business_lifecycle_audit_logs (created_at DESC)""",
            """CREATE INDEX IF NOT EXISTS intent_audit_created_idx
            ON intent_audit_logs (created_at DESC)""",
            """CREATE INDEX IF NOT EXISTS intent_audit_intent_idx
            ON intent_audit_logs (intent, corrected_intent)""",
        )
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() "
                "|| ':business-initialize', 0))"
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """CREATE INDEX IF NOT EXISTS reply_script_embedding_hnsw_idx
                ON reply_script_search_documents USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL"""
            )
            connection.execute(
                """DO $$ BEGIN
                IF to_regclass('product_records') IS NULL THEN
                    DROP TABLE IF EXISTS product_search_documents CASCADE;
                END IF;
                END $$"""
            )
            for statement in (
                "DROP TABLE IF EXISTS business_write_requests",
                "DROP TABLE IF EXISTS catalog_product_media CASCADE",
                "DROP TABLE IF EXISTS product_components CASCADE",
                "DROP TABLE IF EXISTS product_prices CASCADE",
                "DROP TABLE IF EXISTS media_assets CASCADE",
                "DROP TABLE IF EXISTS products CASCADE",
                "DROP TABLE IF EXISTS product_lifecycle_audit_logs CASCADE",
                """CREATE TABLE IF NOT EXISTS product_records (
                    id uuid PRIMARY KEY,
                    sequence_number integer,
                    model varchar(200) NOT NULL,
                    series varchar(200) NOT NULL DEFAULT '',
                    product_description text NOT NULL DEFAULT '',
                    material varchar(200) NOT NULL DEFAULT '',
                    spray_modes varchar(100) NOT NULL DEFAULT '',
                    color varchar(200) NOT NULL DEFAULT '',
                    special_function text NOT NULL DEFAULT '',
                    shipping_origin varchar(200) NOT NULL DEFAULT '',
                    set_price numeric(12,2) CHECK (set_price IS NULL OR set_price >= 0),
                    body_price numeric(12,2) CHECK (body_price IS NULL OR body_price >= 0),
                    cost_price numeric(12,2) CHECK (cost_price IS NULL OR cost_price >= 0),
                    piece_count integer CHECK (piece_count IS NULL OR piece_count >= 0),
                    supplier varchar(300) NOT NULL DEFAULT '',
                    body_supplier varchar(300) NOT NULL DEFAULT '',
                    components jsonb NOT NULL DEFAULT '[]'::jsonb,
                    notes text NOT NULL DEFAULT '',
                    source_row integer,
                    status varchar(30) NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','active','deleted')),
                    deleted_at timestamptz,
                     deleted_from_status varchar(30),
                     published_snapshot jsonb,
                     created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""",
                """CREATE TABLE IF NOT EXISTS product_media_assets (
                    id uuid PRIMARY KEY,
                    original_name varchar(500) NOT NULL,
                    storage_path text NOT NULL,
                    media_type varchar(20) NOT NULL CHECK (media_type IN ('image','video')),
                    content_hash char(64) NOT NULL UNIQUE,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""",
                """ALTER TABLE product_records ADD COLUMN IF NOT EXISTS cost_price
                numeric(12,2) CHECK (cost_price IS NULL OR cost_price >= 0)""",
                "ALTER TABLE product_records ADD COLUMN IF NOT EXISTS published_snapshot jsonb",
                """ALTER TABLE product_records ADD COLUMN IF NOT EXISTS piece_count
                integer CHECK (piece_count IS NULL OR piece_count >= 0)""",
                "ALTER TABLE product_records DROP COLUMN IF EXISTS style",
                """UPDATE product_records
                SET published_snapshot=published_snapshot-'style'
                WHERE published_snapshot ? 'style'""",
                """ALTER TABLE product_records
                DROP COLUMN IF EXISTS main_image_file,
                DROP COLUMN IF EXISTS gallery_image_files,
                DROP COLUMN IF EXISTS configuration_image_file,
                DROP COLUMN IF EXISTS video_file""",
                """CREATE TABLE IF NOT EXISTS product_media_links (
                    product_id uuid NOT NULL REFERENCES product_records(id) ON DELETE CASCADE,
                    media_id uuid NOT NULL REFERENCES product_media_assets(id) ON DELETE CASCADE,
                    role varchar(40) NOT NULL CHECK (role IN
                        ('product_image','body_image','configuration_image','product_video')),
                    sort_order integer NOT NULL DEFAULT 0,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (product_id,media_id,role)
                )""",
                """CREATE TABLE IF NOT EXISTS product_published_media_links (
                    product_id uuid NOT NULL REFERENCES product_records(id) ON DELETE CASCADE,
                    media_id uuid NOT NULL REFERENCES product_media_assets(id) ON DELETE CASCADE,
                    role varchar(40) NOT NULL CHECK (role IN
                        ('product_image','body_image','configuration_image','product_video')),
                    sort_order integer NOT NULL DEFAULT 0,
                    published_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (product_id,media_id,role)
                )""",
                """ALTER TABLE product_media_links
                DROP CONSTRAINT IF EXISTS product_media_links_role_check""",
                """ALTER TABLE product_published_media_links
                DROP CONSTRAINT IF EXISTS product_published_media_links_role_check""",
                """UPDATE product_media_links SET role=CASE role
                    WHEN 'main_image' THEN 'product_image'
                    WHEN 'gallery_image' THEN 'body_image'
                    ELSE role END
                WHERE role IN ('main_image','gallery_image')""",
                """UPDATE product_published_media_links SET role=CASE role
                    WHEN 'main_image' THEN 'product_image'
                    WHEN 'gallery_image' THEN 'body_image'
                    ELSE role END
                WHERE role IN ('main_image','gallery_image')""",
                """ALTER TABLE product_media_links ADD CONSTRAINT
                product_media_links_role_check CHECK (role IN
                    ('product_image','body_image','configuration_image','product_video'))""",
                """ALTER TABLE product_published_media_links ADD CONSTRAINT
                product_published_media_links_role_check CHECK (role IN
                    ('product_image','body_image','configuration_image','product_video'))""",
                f"""CREATE TABLE IF NOT EXISTS product_search_documents (
                    product_id uuid PRIMARY KEY REFERENCES product_records(id) ON DELETE CASCADE,
                    search_text text NOT NULL DEFAULT '',
                    embedding vector({dimensions}),
                    embedding_model varchar(200),
                    embedding_status varchar(30) NOT NULL DEFAULT 'not_requested',
                    embedding_error text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )""",
                """ALTER TABLE product_records ADD COLUMN IF NOT EXISTS components
                jsonb NOT NULL DEFAULT '[]'::jsonb""",
                """UPDATE product_records p SET published_snapshot=jsonb_build_object(
                    'sequence_number',p.sequence_number,'model',p.model,'series',p.series,
                    'product_description',p.product_description,
                    'material',p.material,'spray_modes',p.spray_modes,'color',p.color,
                    'special_function',p.special_function,'shipping_origin',p.shipping_origin,
                    'set_price',p.set_price,'cost_price',p.cost_price,'body_price',p.body_price,
                    'piece_count',p.piece_count,
                    'supplier',p.supplier,'body_supplier',p.body_supplier,
                    'components',p.components,'notes',p.notes,'source_row',p.source_row,
                    'media_ids',COALESCE((SELECT jsonb_agg(
                        l.media_id::text ORDER BY l.sort_order,l.media_id)
                        FROM product_media_links l WHERE l.product_id=p.id),'[]'::jsonb))
                WHERE p.status='active' AND p.published_snapshot IS NULL""",
                """CREATE OR REPLACE VIEW active_product_catalog AS
                SELECT p.id,
                NULLIF(p.published_snapshot->>'sequence_number','')::integer
                    AS sequence_number,
                COALESCE(p.published_snapshot->>'model','') AS model,
                COALESCE(p.published_snapshot->>'series','') AS series,
                COALESCE(p.published_snapshot->>'product_description','')
                    AS product_description,
                COALESCE(p.published_snapshot->>'material','') AS material,
                COALESCE(p.published_snapshot->>'spray_modes','') AS spray_modes,
                COALESCE(p.published_snapshot->>'color','') AS color,
                COALESCE(p.published_snapshot->>'special_function','') AS special_function,
                COALESCE(p.published_snapshot->>'shipping_origin','') AS shipping_origin,
                NULLIF(p.published_snapshot->>'set_price','')::numeric AS set_price,
                NULLIF(p.published_snapshot->>'cost_price','')::numeric AS cost_price,
                NULLIF(p.published_snapshot->>'body_price','')::numeric AS body_price,
                NULLIF(p.published_snapshot->>'piece_count','')::integer AS piece_count,
                COALESCE(p.published_snapshot->>'supplier','') AS supplier,
                COALESCE(p.published_snapshot->>'body_supplier','') AS body_supplier,
                COALESCE(p.published_snapshot->'components','[]'::jsonb) AS components,
                COALESCE(p.published_snapshot->>'notes','') AS notes,
                NULLIF(p.published_snapshot->>'source_row','')::integer AS source_row,
                p.created_at,p.updated_at
                FROM product_records p
                WHERE p.status='active' AND p.published_snapshot IS NOT NULL""",
                """INSERT INTO product_published_media_links(product_id,media_id,role,sort_order)
                SELECT l.product_id,l.media_id,l.role,l.sort_order
                FROM product_media_links l JOIN product_records p ON p.id=l.product_id
                WHERE p.status='active'
                ON CONFLICT DO NOTHING""",
                "DROP INDEX IF EXISTS product_records_model_idx",
                """DO $$ DECLARE duplicate_models text; BEGIN
                SELECT string_agg(model_key, '、' ORDER BY model_key) INTO duplicate_models
                FROM (
                    SELECT lower(btrim(model)) AS model_key
                    FROM product_records
                    GROUP BY lower(btrim(model)) HAVING count(*) > 1
                    LIMIT 20
                ) duplicates;
                IF duplicate_models IS NOT NULL THEN
                    RAISE EXCEPTION '产品型号存在重复，无法启用唯一约束：%', duplicate_models;
                END IF;
                END $$""",
                """CREATE UNIQUE INDEX IF NOT EXISTS product_records_model_unique_idx
                ON product_records (lower(btrim(model)))""",
                "DROP INDEX IF EXISTS product_records_search_idx",
                """CREATE INDEX IF NOT EXISTS product_records_search_idx ON product_records
                USING gin ((model || ' ' || series || ' ' || material || ' ' ||
                color || ' ' || special_function) gin_trgm_ops)""",
                """CREATE INDEX IF NOT EXISTS product_media_links_product_idx
                ON product_media_links (product_id,role,sort_order)""",
                """CREATE INDEX IF NOT EXISTS product_published_media_links_product_idx
                ON product_published_media_links (product_id,role,sort_order)""",
                """CREATE INDEX IF NOT EXISTS product_search_text_idx
                ON product_search_documents USING gin (search_text gin_trgm_ops)""",
            ):
                connection.execute(statement)
            connection.execute(
                """CREATE INDEX IF NOT EXISTS product_search_embedding_hnsw_idx
                ON product_search_documents USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL"""
            )

    def get_wecom_media_id(self, content_hash: str, media_type: str) -> str | None:
        """仅返回仍有至少六小时安全余量的企业微信临时素材凭证。"""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT media_id FROM wecom_media_cache
                WHERE content_hash=%s AND media_type=%s
                AND expires_at > now() + interval '6 hours'""",
                (content_hash, media_type),
            ).fetchone()
        return str(row["media_id"]) if row else None

    def save_wecom_media_id(self, content_hash: str, media_type: str, media_id: str) -> None:
        """企业微信临时素材有效三天；数据库保存有效期用于自动续传。"""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO wecom_media_cache
                (content_hash,media_type,media_id,expires_at)
                VALUES (%s,%s,%s,now() + interval '3 days')
                ON CONFLICT (content_hash,media_type) DO UPDATE SET
                media_id=excluded.media_id,created_at=now(),
                expires_at=excluded.expires_at,updated_at=now()""",
                (content_hash, media_type, media_id),
            )

    def invalidate_wecom_media_id(self, content_hash: str, media_type: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM wecom_media_cache
                WHERE content_hash=%s AND media_type=%s""",
                (content_hash, media_type),
            )

    @staticmethod
    def _reply_script_search_text(row: dict[str, object]) -> str:
        values = (
            row.get("title"),
            row.get("scenario"),
            row.get("intent"),
            row.get("emotion"),
            row.get("customer_question"),
            row.get("employee_reply") or row.get("content"),
            row.get("conditions"),
            row.get("product_scope"),
        )
        return "；".join(str(value).strip() for value in values if str(value or "").strip())

    def reply_scripts_needing_embeddings(self, model: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.id::text,r.title,r.scenario,r.intent,r.emotion,
                r.customer_question,r.employee_reply,r.content,r.conditions,r.product_scope,
                sd.search_text,sd.embedding_model,(sd.embedding IS NOT NULL) AS has_embedding
                FROM reply_scripts r LEFT JOIN reply_script_search_documents sd
                ON sd.script_id=r.id
                WHERE r.status='published' AND r.is_current=true"""
            ).fetchall()
        pending: list[dict[str, str]] = []
        for row in rows:
            search_text = self._reply_script_search_text(dict(row))
            if (
                not row["has_embedding"]
                or row["embedding_model"] != model
                or row["search_text"] != search_text
            ):
                pending.append({"id": row["id"], "search_text": search_text})
        return pending

    def save_reply_script_embeddings(
        self, records: list[tuple[str, str, list[float]]], model: str
    ) -> None:
        if not records:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO reply_script_search_documents
                (script_id,search_text,embedding,embedding_model,embedding_status,
                embedding_error) VALUES (%s,%s,%s,%s,'ready',NULL)
                ON CONFLICT (script_id) DO UPDATE SET search_text=excluded.search_text,
                embedding=excluded.embedding,embedding_model=excluded.embedding_model,
                embedding_status='ready',embedding_error=NULL,updated_at=now()""",
                [
                    (script_id, search_text, Vector(embedding), model)
                    for script_id, search_text, embedding in records
                ],
            )

    def create_script(
        self,
        title: str,
        scenario: str,
        audience: str,
        content: str,
        conditions: str = "",
        required_fields: list[str] | None = None,
        forbidden_phrases: list[str] | None = None,
        variables: list[str] | None = None,
        product_scope: str = "",
        validation_warnings: list[str] | None = None,
        customer_question: str = "",
        employee_reply: str = "",
        intent: str = "",
        emotion: str = "",
        quality_score: float | None = None,
        source_row: int | None = None,
    ) -> dict[str, object]:
        script_id = str(uuid4())
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO reply_scripts
                (id,title,scenario,audience,content,conditions,required_fields,
                forbidden_phrases,variables,product_scope,validation_warnings,
                customer_question,employee_reply,intent,emotion,quality_score,source_row,logical_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *""",
                (
                    script_id,
                    title,
                    scenario,
                    audience,
                    content,
                    conditions,
                    Jsonb(required_fields or []),
                    Jsonb(forbidden_phrases or []),
                    Jsonb(variables or []),
                    product_scope,
                    Jsonb(validation_warnings or []),
                    customer_question,
                    employee_reply,
                    intent,
                    emotion,
                    quality_score,
                    source_row,
                    script_id,
                ),
            ).fetchone()
        return dict(row)

    def create_script_version(
        self, script_id: str, values: dict[str, object]
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM reply_scripts WHERE id=%s AND status<>'deleted'",
                (script_id,),
            ).fetchone()
            if not current:
                return None
            next_version = int(current["version_number"]) + 1
            version_id = str(uuid4())
            row = connection.execute(
                """INSERT INTO reply_scripts
                (id,title,scenario,audience,content,conditions,required_fields,
                forbidden_phrases,variables,product_scope,logical_id,version_number,
                validation_warnings,customer_question,employee_reply,intent,emotion,
                quality_score,source_row,is_current,supersedes_id,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false,%s,'draft')
                RETURNING *""",
                (
                    version_id,
                    values.get("title", current["title"]),
                    values.get("scenario", current["scenario"]),
                    values.get("audience", current["audience"]),
                    values.get("content", current["content"]),
                    values.get("conditions", current["conditions"]),
                    Jsonb(values.get("required_fields", current["required_fields"])),
                    Jsonb(values.get("forbidden_phrases", current["forbidden_phrases"])),
                    Jsonb(values.get("variables", current["variables"])),
                    values.get("product_scope", current["product_scope"]),
                    current["logical_id"],
                    next_version,
                    Jsonb(values.get("validation_warnings", current["validation_warnings"])),
                    values.get("customer_question", current["customer_question"]),
                    values.get("employee_reply", current["employee_reply"]),
                    values.get("intent", current["intent"]),
                    values.get("emotion", current["emotion"]),
                    values.get("quality_score", current["quality_score"]),
                    values.get("source_row", current["source_row"]),
                    script_id,
                ),
            ).fetchone()
        return dict(row)

    def save_script_edit(
        self, script_id: str, values: dict[str, object]
    ) -> dict[str, object] | None:
        """草稿原地修改；非草稿生成或更新一份待发布的新版本。"""
        with self._connect() as connection:
            source = connection.execute(
                "SELECT * FROM reply_scripts WHERE id=%s AND status<>'deleted'",
                (script_id,),
            ).fetchone()
            if not source:
                return None

            draft = (
                source
                if source["status"] == "draft"
                else connection.execute(
                    """SELECT * FROM reply_scripts
                WHERE logical_id=%s AND status='draft'
                ORDER BY version_number DESC,updated_at DESC LIMIT 1""",
                    (source["logical_id"],),
                ).fetchone()
            )
            if draft:
                row = connection.execute(
                    """UPDATE reply_scripts SET
                    customer_question=%s,employee_reply=%s,content=%s,scenario=%s,
                    intent=%s,emotion=%s,quality_score=%s,validation_warnings=%s,
                    updated_at=now() WHERE id=%s RETURNING *""",
                    (
                        values["customer_question"],
                        values["employee_reply"],
                        values["content"],
                        values["scenario"],
                        values["intent"],
                        values["emotion"],
                        values["quality_score"],
                        Jsonb(values.get("validation_warnings", [])),
                        draft["id"],
                    ),
                ).fetchone()
                result = dict(row)
                result["edit_result"] = "draft_updated"
                return result

            next_version = connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 AS value "
                "FROM reply_scripts WHERE logical_id=%s",
                (source["logical_id"],),
            ).fetchone()["value"]
            version_id = str(uuid4())
            row = connection.execute(
                """INSERT INTO reply_scripts
                (id,title,scenario,audience,content,visibility,status,conditions,
                required_fields,forbidden_phrases,variables,product_scope,
                validation_warnings,customer_question,employee_reply,intent,emotion,
                quality_score,source_row,logical_id,version_number,is_current,supersedes_id)
                VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,false,%s) RETURNING *""",
                (
                    version_id,
                    source["title"],
                    values["scenario"],
                    source["audience"],
                    values["content"],
                    source["visibility"],
                    source["conditions"],
                    Jsonb(source["required_fields"]),
                    Jsonb(source["forbidden_phrases"]),
                    Jsonb(source["variables"]),
                    source["product_scope"],
                    Jsonb(values.get("validation_warnings", [])),
                    values["customer_question"],
                    values["employee_reply"],
                    values["intent"],
                    values["emotion"],
                    values["quality_score"],
                    source["source_row"],
                    source["logical_id"],
                    next_version,
                    source["id"],
                ),
            ).fetchone()
        result = dict(row)
        result["edit_result"] = "version_created"
        return result

    def publish_script(self, script_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            current = connection.execute(
                """WITH group_lock AS (
                    SELECT pg_advisory_xact_lock(hashtextextended(logical_id::text,0))
                    FROM reply_scripts WHERE id=%s AND status<>'deleted'
                )
                SELECT logical_id::text AS logical_id FROM reply_scripts,group_lock
                WHERE id=%s AND status<>'deleted' FOR UPDATE""",
                (script_id, script_id),
            ).fetchone()
            if not current:
                return None
            connection.execute(
                """UPDATE reply_scripts SET deleted_from_status=status,status='deleted',
                is_current=false,deleted_at=now(),updated_at=now()
                WHERE logical_id=%s AND id<>%s AND status<>'deleted'""",
                (current["logical_id"], script_id),
            )
            row = connection.execute(
                """UPDATE reply_scripts SET status='published',is_current=true,updated_at=now()
                WHERE id=%s RETURNING *""",
                (script_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_script(self, script_id: str) -> dict[str, object] | None:
        """将同一条话术的使用中版本和待发布修改一并移入回收站。"""
        with self._connect() as connection:
            current = connection.execute(
                """SELECT logical_id FROM reply_scripts
                WHERE id=%s AND status<>'deleted'""",
                (script_id,),
            ).fetchone()
            if not current:
                return None
            row = connection.execute(
                """UPDATE reply_scripts SET deleted_from_status=status,status='deleted',
                is_current=false,deleted_at=now(),updated_at=now()
                WHERE logical_id=%s AND status<>'deleted' RETURNING *""",
                (current["logical_id"],),
            ).fetchone()
        return dict(row) if row else None

    def delete_scripts(self, script_ids: list[str]) -> int:
        """在一个事务中将所选话术逻辑组移入回收站。"""
        unique_ids = list(dict.fromkeys(script_ids))
        if not unique_ids:
            return 0
        if len(unique_ids) > 100:
            raise ValueError("每次最多批量删除 100 条话术")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT logical_id::text AS logical_id FROM reply_scripts
                WHERE id=ANY(%s::uuid[]) AND status<>'deleted'""",
                (unique_ids,),
            ).fetchall()
            logical_ids = [row["logical_id"] for row in rows]
            if not logical_ids:
                return 0
            connection.execute(
                """UPDATE reply_scripts SET deleted_from_status=status,status='deleted',
                is_current=false,deleted_at=now(),updated_at=now()
                WHERE logical_id=ANY(%s::uuid[]) AND status<>'deleted'""",
                (logical_ids,),
            )
        return len(logical_ids)

    def restore_script(self, script_id: str) -> dict[str, object] | None:
        """恢复话术；历史版本恢复时与当前发布版本安全互换。"""
        with self._connect() as connection:
            current = connection.execute(
                """WITH group_lock AS (
                    SELECT pg_advisory_xact_lock(hashtextextended(logical_id::text,0))
                    FROM reply_scripts WHERE id=%s AND status='deleted'
                )
                SELECT logical_id,deleted_from_status FROM reply_scripts,group_lock
                WHERE id=%s AND status='deleted' FOR UPDATE""",
                (script_id, script_id),
            ).fetchone()
            if not current:
                return None
            live = connection.execute(
                """SELECT 1 FROM reply_scripts WHERE logical_id=%s
                AND status<>'deleted' LIMIT 1""",
                (current["logical_id"],),
            ).fetchone()
            if live:
                if current["deleted_from_status"] == "published":
                    connection.execute(
                        """UPDATE reply_scripts SET deleted_from_status='published',
                        status='deleted',is_current=false,deleted_at=now(),updated_at=now()
                        WHERE logical_id=%s AND status='published'""",
                        (current["logical_id"],),
                    )
                row = connection.execute(
                    """UPDATE reply_scripts SET
                    status=COALESCE(NULLIF(deleted_from_status,''),'draft'),
                    is_current=(deleted_from_status='published'),
                    deleted_from_status=NULL,deleted_at=NULL,updated_at=now()
                    WHERE id=%s AND status='deleted' RETURNING *""",
                    (script_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """UPDATE reply_scripts SET
                    status=COALESCE(NULLIF(deleted_from_status,''),'draft'),
                    is_current=(deleted_from_status='published'),
                    deleted_from_status=NULL,deleted_at=NULL,updated_at=now()
                    WHERE id=%s AND status='deleted' RETURNING *""",
                    (script_id,),
                ).fetchone()
        return dict(row) if row else None

    def permanently_delete_script(self, script_id: str, actor: str = "admin") -> bool:
        """按具体版本或逻辑组永久删除；有在用版本时仅删除选中的历史版本。"""
        with self._connect() as connection:
            current = connection.execute(
                """SELECT id::text,logical_id::text FROM reply_scripts
                WHERE status='deleted' AND (id=%s OR logical_id=%s)
                ORDER BY (id=%s) DESC,version_number DESC LIMIT 1""",
                (script_id, script_id, script_id),
            ).fetchone()
            if not current:
                return False
            target_id = current["id"]
            result = connection.execute(
                """DELETE FROM reply_scripts r WHERE
                (r.id=%s AND EXISTS (
                    SELECT 1 FROM reply_scripts sibling
                    WHERE sibling.logical_id=%s AND sibling.status<>'deleted'
                )) OR
                (r.logical_id=%s AND NOT EXISTS (
                    SELECT 1 FROM reply_scripts sibling
                    WHERE sibling.logical_id=%s AND sibling.status<>'deleted'
                ))""",
                (
                    target_id,
                    current["logical_id"],
                    current["logical_id"],
                    current["logical_id"],
                ),
            )
            connection.execute(
                """INSERT INTO business_lifecycle_audit_logs
                (id,resource_type,resource_id,action,actor)
                VALUES (%s,'reply_script',%s,'permanent_delete',%s)""",
                (str(uuid4()), target_id, actor),
            )
        return result.rowcount > 0

    def purge_expired_reply_scripts(self, retention_days: int = 7) -> int:
        """逐条永久清理超过回收站保留期的话术及其检索向量。"""
        with self._connect() as connection:
            rows = connection.execute(
                """DELETE FROM reply_scripts WHERE status='deleted'
                AND deleted_at < now() - make_interval(days => %s)
                RETURNING id::text""",
                (retention_days,),
            ).fetchall()
            if rows:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO business_lifecycle_audit_logs
                        (id,resource_type,resource_id,action,actor)
                        VALUES (%s,'reply_script',%s,'permanent_delete','system')""",
                        [(str(uuid4()), row["id"]) for row in rows],
                    )
        return len(rows)

    def trash_stale_reply_script_drafts(self, retention_days: int = 7) -> int:
        """将七天未修改的话术草稿移入回收站，不影响同组已发布版本。"""
        with self._connect() as connection:
            result = connection.execute(
                """UPDATE reply_scripts SET deleted_from_status='draft',status='deleted',
                is_current=false,deleted_at=now(),updated_at=now()
                WHERE status='draft'
                  AND updated_at < now() - make_interval(days => %s)""",
                (retention_days,),
            )
        return result.rowcount

    def list_records(self, table: str) -> list[dict[str, object]]:
        allowed = {"reply_scripts", "product_records", "product_media_assets"}
        if table not in allowed:
            raise ValueError("不支持的数据表")
        with self._connect() as connection:
            rows = connection.execute(
                psycopg.sql.SQL("SELECT * FROM {} ORDER BY 1").format(psycopg.sql.Identifier(table))
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reply_scripts(self, include_deleted: bool = False) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM reply_scripts
                WHERE (%s OR status<>'deleted')
                ORDER BY created_at,id""",
                (include_deleted,),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_scripts(
        self,
        query: str,
        limit: int = 3,
        audience: str = "customer",
        query_embedding: list[float] | None = None,
    ) -> list[BusinessResult]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.id::text,r.title,r.content,r.scenario,r.intent,r.emotion,
                r.customer_question,r.employee_reply,r.conditions,r.product_scope,
                r.quality_score
                FROM reply_scripts r
                WHERE r.status='published' AND r.is_current=true
                AND r.audience=%s AND r.visibility IN ('public','internal')""",
                (audience,),
            ).fetchall()
            semantic_scores: dict[str, float] = {}
            if query_embedding:
                vector = Vector(query_embedding)
                semantic_rows = connection.execute(
                    """SELECT r.id::text,
                    (1-(sd.embedding <=> %s))::float AS semantic_score
                    FROM reply_scripts r JOIN reply_script_search_documents sd
                    ON sd.script_id=r.id
                    WHERE r.status='published' AND r.is_current=true
                    AND r.audience=%s AND r.visibility IN ('public','internal')
                    AND sd.embedding IS NOT NULL
                    ORDER BY sd.embedding <=> %s LIMIT %s""",
                    (vector, audience, vector, max(limit * 8, 20)),
                ).fetchall()
                semantic_scores = {row["id"]: float(row["semantic_score"]) for row in semantic_rows}
        rows_by_id = {row["id"]: row for row in rows}
        documents = {row["id"]: self._reply_script_search_text(dict(row)) for row in rows}
        quality_scores = {row["id"]: float(row["quality_score"] or 0.0) for row in rows}
        candidate_limit = max(limit * 8, 20)
        matches = hybrid_rerank(
            query,
            documents,
            semantic_scores,
            limit=candidate_limit,
            candidate_limit=candidate_limit,
            quality_scores=quality_scores,
        )
        matches = rerank_matches(query, documents, matches, limit=limit)
        return [
            BusinessResult(
                "reply_script",
                match.key,
                rows_by_id[match.key]["title"],
                rows_by_id[match.key]["content"],
                match.score,
            )
            for match in matches
        ]

    @staticmethod
    def as_dict(result: BusinessResult) -> dict[str, object]:
        return asdict(result)

    def record_intent_audit(
        self,
        *,
        user_id: str,
        query: str,
        intent: str,
        confidence: float,
        intent_source: str,
        intent_reason: str,
        understood_query: str,
        data_source: str,
        response_preview: str,
        source_count: int,
        response_ms: float,
        request_id: str | None = None,
        planner_version: str = "",
        query_plan_redacted: object | None = None,
        validation_result: object | None = None,
        execution_summary: object | None = None,
        shadow_diff: object | None = None,
    ) -> str:
        audit_id = str(uuid4())
        request_fingerprint = text_fingerprint(request_id) if request_id else None
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO intent_audit_logs
                (id,request_fingerprint,user_fingerprint,query_fingerprint,query_redacted,
                intent,confidence,
                intent_source,intent_reason,understood_query_redacted,data_source,
                response_preview,source_count,response_ms,planner_version,
                query_plan_redacted,validation_result,execution_summary,shadow_diff)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (request_fingerprint) WHERE request_fingerprint IS NOT NULL
                DO NOTHING RETURNING id""",
                (
                    audit_id,
                    request_fingerprint,
                    text_fingerprint(user_id),
                    text_fingerprint(query),
                    normalize_audit_text(query),
                    intent,
                    max(0.0, min(1.0, confidence)),
                    normalize_audit_text(intent_source, 60),
                    normalize_audit_text(intent_reason, 300),
                    normalize_audit_text(understood_query),
                    normalize_audit_text(data_source, 200) or "none",
                    normalize_audit_text(response_preview, 800),
                    max(0, int(source_count)),
                    max(0.0, float(response_ms)),
                    normalize_audit_text(planner_version, 60),
                    Jsonb(query_plan_redacted or {}),
                    Jsonb(validation_result or {}),
                    Jsonb(execution_summary or {}),
                    Jsonb(shadow_diff or {}),
                ),
            ).fetchone()
            if row:
                return str(row["id"])
            existing = connection.execute(
                """SELECT id FROM intent_audit_logs
                WHERE request_fingerprint = %s""",
                (request_fingerprint,),
            ).fetchone()
            if existing:
                return str(existing["id"])
        return audit_id

    def list_intent_audits(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        intent: str | None = None,
        corrected: bool | None = None,
        min_confidence: float | None = None,
    ) -> list[dict[str, object]]:
        clauses = ["1=1"]
        params: list[object] = []
        if intent:
            clauses.append("intent=%s")
            params.append(intent)
        if corrected is True:
            clauses.append("corrected_intent IS NOT NULL")
        elif corrected is False:
            clauses.append("corrected_intent IS NULL")
        if min_confidence is not None:
            clauses.append("confidence >= %s")
            params.append(min_confidence)
        params.extend([max(1, min(200, limit)), max(0, offset)])
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT id::text,created_at,user_fingerprint,query_redacted,intent,
                confidence,intent_source,intent_reason,understood_query_redacted,data_source,
                response_preview,source_count,response_ms,corrected_intent,correction_reason,
                corrected_by,corrected_at,planner_version,query_plan_redacted,
                validation_result,execution_summary,shadow_diff
                FROM intent_audit_logs WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_intent_audit(self, audit_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id::text,created_at,user_fingerprint,query_redacted,intent,
                confidence,intent_source,intent_reason,understood_query_redacted,data_source,
                response_preview,source_count,response_ms,corrected_intent,correction_reason,
                corrected_by,corrected_at,planner_version,query_plan_redacted,
                validation_result,execution_summary,shadow_diff
                FROM intent_audit_logs WHERE id=%s""",
                (audit_id,),
            ).fetchone()
        return dict(row) if row else None

    def correct_intent_audit(
        self, audit_id: str, corrected_intent: str | None, reason: str, corrected_by: str
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE intent_audit_logs SET corrected_intent=%s,
                correction_reason=%s,corrected_by=%s,corrected_at=now()
                WHERE id=%s RETURNING id::text,created_at,user_fingerprint,query_redacted,
                intent,confidence,intent_source,intent_reason,understood_query_redacted,
                data_source,response_preview,source_count,response_ms,corrected_intent,
                correction_reason,corrected_by,corrected_at,planner_version,
                query_plan_redacted,validation_result,execution_summary,shadow_diff""",
                (corrected_intent, reason[:500], corrected_by[:100], audit_id),
            ).fetchone()
        return dict(row) if row else None
