"""Database operations for the spreadsheet-shaped product archive."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from pgvector import Vector
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.business.product_query import PRODUCT_FIELD_CATALOG, ProductFilter, ProductQuery
from app.business.product_selection import (
    has_product_identifier,
    mentions_identifier,
    mentions_identifier_alias,
    narrow_variants,
)
from app.retrieval import hybrid_rerank


class ProductModelConflictError(ValueError):
    """Raised when a normalized product model is already reserved."""

    def __init__(self, model: str) -> None:
        super().__init__(f"产品型号“{model}”已存在；产品型号全局唯一（不区分大小写）")
        self.model = model


@dataclass(frozen=True)
class ProductQueryResult:
    """One stable, database-paginated projection of the published catalog."""

    items: list[dict[str, object]]
    total: int
    page: int
    page_size: int
    # Unified response metadata. Defaults keep compatibility with existing in-memory stores.
    has_more: bool = False
    cursor: str | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    requested_fields: tuple[str, ...] = ()


# 统计类查询一次最多取回多少行。统计要把匹配项全部取回来算，所以不按展示分页来限；
# 但仍设上限，避免库规模变大时把整库拉进内存。
AGGREGATE_MAX_ROWS = 500


_QUERY_COLUMNS = {
    "model": "model",
    "series": "series",
    "description": "product_description",
    "material": "material",
    "spray_modes": "spray_modes",
    "color": "color",
    "special_function": "special_function",
    "shipping_origin": "shipping_origin",
    "set_price": "set_price",
    "cost_price": "cost_price",
    "body_price": "body_price",
    "piece_count": "piece_count",
    "supplier": "supplier",
    "body_supplier": "body_supplier",
    "components": "components",
}
_TEXT_EMPTY_VALUES = ("", "无", "未登记", "暂无", "资料中未登记")


def _escape_like(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _filter_sql(item: ProductFilter, params: list[object]) -> str:
    column = _QUERY_COLUMNS[item.field]
    definition = PRODUCT_FIELD_CATALOG[item.field]
    if item.operator == "is_present":
        if definition.value_type == "number":
            return f"{column} IS NOT NULL"
        if definition.value_type == "json":
            return f"{column} IS NOT NULL AND {column} <> '[]'::jsonb"
        params.append(list(_TEXT_EMPTY_VALUES))
        return f"lower(btrim({column})) <> ALL(%s)"
    if item.operator == "is_empty":
        if definition.value_type == "number":
            return f"{column} IS NULL"
        if definition.value_type == "json":
            return f"{column} IS NULL OR {column} = '[]'::jsonb"
        params.append(list(_TEXT_EMPTY_VALUES))
        return f"lower(btrim({column})) = ANY(%s)"
    if item.operator == "contains":
        params.append(f"%{_escape_like(item.value)}%")
        return f"{column}::text ILIKE %s ESCAPE E'\\\\'"
    if item.operator == "equals":
        params.append(item.value)
        return f"{column} = %s"
    if item.operator == "less_than":
        params.append(item.value)
        return f"{column} < %s"
    if item.operator == "less_than_or_equal":
        params.append(item.value)
        return f"{column} <= %s"
    if item.operator == "greater_than":
        params.append(item.value)
        return f"{column} > %s"
    if item.operator == "greater_than_or_equal":
        params.append(item.value)
        return f"{column} >= %s"
    values = _sequence(item.value)
    if item.operator == "between":
        if len(values) != 2:
            raise ValueError(f"字段 {item.field} 的区间值必须包含两项")
        params.extend(values)
        return f"{column} BETWEEN %s AND %s"
    if item.operator == "in":
        if not values:
            return "FALSE"
        placeholders = ",".join("%s" for _ in values)
        params.extend(values)
        return f"{column} IN ({placeholders})"
    raise ValueError(f"不支持的产品筛选操作符：{item.operator}")


class ProductStoreMixin:
    _PRODUCT_FIELDS = (
        "sequence_number",
        "model",
        "series",
        "product_description",
        "material",
        "spray_modes",
        "color",
        "special_function",
        "shipping_origin",
        "set_price",
        "cost_price",
        "body_price",
        "piece_count",
        "supplier",
        "body_supplier",
        "components",
        "notes",
        "source_row",
    )

    @classmethod
    def _published_values(cls, record: dict[str, object]) -> dict[str, object]:
        def json_safe(value: object) -> object:
            if isinstance(value, Decimal):
                return float(value)
            if isinstance(value, list):
                return [json_safe(item) for item in value]
            if isinstance(value, dict):
                return {key: json_safe(item) for key, item in value.items()}
            return value

        return {key: json_safe(record.get(key)) for key in cls._PRODUCT_FIELDS}

    @classmethod
    def _active_projection(cls, record: dict[str, object]) -> dict[str, object]:
        snapshot = record.get("published_snapshot")
        if record.get("status") == "active" and isinstance(snapshot, dict):
            projected = dict(record)
            projected.update({key: snapshot.get(key) for key in cls._PRODUCT_FIELDS})
            return projected
        return record

    @staticmethod
    def _components_json(values: object) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in list(values or []):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            if copied.get("unit_price") is not None:
                copied["unit_price"] = float(copied["unit_price"])
            result.append(copied)
        return result

    def _product_record_row(
        self, connection, product_id: str, *, published: bool = False
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT * FROM product_records WHERE id=%s", (product_id,)
        ).fetchone()
        if not row:
            return None
        media_table = "product_published_media_links" if published else "product_media_links"
        media = connection.execute(
            f"""SELECT a.id::text,a.original_name,a.storage_path,a.media_type,a.content_hash,
            l.role,l.sort_order,a.created_at
            FROM {media_table} l JOIN product_media_assets a ON a.id=l.media_id
            WHERE l.product_id=%s
            ORDER BY CASE l.role WHEN 'product_image' THEN 0 WHEN 'body_image' THEN 1
            WHEN 'configuration_image' THEN 2 ELSE 3 END,l.sort_order,a.created_at""",
            (product_id,),
        ).fetchall()
        result = dict(row)
        result["id"] = str(result["id"])
        result["media"] = [dict(item) for item in media]
        return result

    def create_product_record(self, values: dict[str, object]) -> dict[str, object]:
        product_id = str(uuid4())
        normalized = self._normalize_product_values(values)
        try:
            with self._connect() as connection:
                self._insert_product_record(connection, product_id, normalized)
                return self._product_record_row(connection, product_id) or {}
        except UniqueViolation as exc:
            raise ProductModelConflictError(str(normalized["model"])) from exc

    @staticmethod
    def _normalize_product_values(values: dict[str, object]) -> dict[str, object]:
        normalized = dict(values)
        model = str(normalized.get("model") or "").strip()
        if not model:
            raise ValueError("产品型号不能为空")
        normalized["model"] = model
        return normalized

    @staticmethod
    def _insert_product_record(connection, product_id: str, values: dict[str, object]) -> None:
        connection.execute(
            """INSERT INTO product_records
            (id,sequence_number,model,series,product_description,material,spray_modes,
            color,special_function,shipping_origin,set_price,cost_price,body_price,supplier,
            body_supplier,components,notes,source_row,status,piece_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                product_id,
                values.get("sequence_number"),
                values["model"],
                values.get("series", ""),
                values.get("product_description", ""),
                values.get("material", ""),
                values.get("spray_modes", ""),
                values.get("color", ""),
                values.get("special_function", ""),
                values.get("shipping_origin", ""),
                values.get("set_price"),
                values.get("cost_price"),
                values.get("body_price"),
                values.get("supplier", ""),
                values.get("body_supplier", ""),
                Jsonb(ProductStoreMixin._components_json(values.get("components"))),
                values.get("notes", ""),
                values.get("source_row"),
                values.get("status", "draft"),
                values.get("piece_count"),
            ),
        )

    def import_product_records(self, records: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized_records = [self._normalize_product_values(values) for values in records]
        by_key: dict[str, list[str]] = {}
        for values in normalized_records:
            model = str(values["model"])
            by_key.setdefault(model.casefold(), []).append(model)
        duplicate = next((items[0] for items in by_key.values() if len(items) > 1), None)
        if duplicate:
            raise ProductModelConflictError(duplicate)
        created: list[dict[str, object]] = []
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT model FROM product_records WHERE lower(btrim(model))=ANY(%s)",
                    (list(by_key),),
                ).fetchone()
                if existing:
                    raise ProductModelConflictError(str(existing["model"]))
                for values in normalized_records:
                    product_id = str(uuid4())
                    self._insert_product_record(connection, product_id, values)
                    product = self._product_record_row(connection, product_id)
                    if product:
                        created.append(product)
        except UniqueViolation as exc:
            model = str(normalized_records[0]["model"]) if normalized_records else "未知型号"
            raise ProductModelConflictError(model) from exc
        return created

    def list_product_records(self, include_deleted: bool = False) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id::text FROM product_records
                WHERE (%s OR status<>'deleted')
                ORDER BY sequence_number NULLS LAST,source_row NULLS LAST,created_at,id""",
                (include_deleted,),
            ).fetchall()
            return [
                item for row in rows if (item := self._product_record_row(connection, row["id"]))
            ]

    def get_product_record(self, product_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            return self._product_record_row(connection, product_id)

    def update_product_record(
        self, product_id: str, values: dict[str, object]
    ) -> dict[str, object] | None:
        normalized = self._normalize_product_values(values)
        try:
            with self._connect() as connection:
                row = connection.execute(
                """UPDATE product_records SET sequence_number=%s,model=%s,series=%s,
                product_description=%s,material=%s,spray_modes=%s,color=%s,
                special_function=%s,shipping_origin=%s,set_price=%s,cost_price=%s,body_price=%s,
                supplier=%s,body_supplier=%s,components=%s,notes=%s,source_row=%s,
                piece_count=CASE WHEN %s THEN %s ELSE piece_count END,updated_at=now()
                WHERE id=%s AND status<>'deleted' RETURNING id""",
                (
                    normalized.get("sequence_number"),
                    normalized["model"],
                    normalized.get("series", ""),
                    normalized.get("product_description", ""),
                    normalized.get("material", ""),
                    normalized.get("spray_modes", ""),
                    normalized.get("color", ""),
                    normalized.get("special_function", ""),
                    normalized.get("shipping_origin", ""),
                    normalized.get("set_price"),
                    normalized.get("cost_price"),
                    normalized.get("body_price"),
                    normalized.get("supplier", ""),
                    normalized.get("body_supplier", ""),
                    Jsonb(ProductStoreMixin._components_json(normalized.get("components"))),
                    normalized.get("notes", ""),
                    normalized.get("source_row"),
                    "piece_count" in normalized,
                    normalized.get("piece_count"),
                    product_id,
                ),
                ).fetchone()
                return self._product_record_row(connection, product_id) if row else None
        except UniqueViolation as exc:
            raise ProductModelConflictError(str(normalized["model"])) from exc

    def set_product_status(self, product_id: str, status: str) -> dict[str, object] | None:
        if status not in {"draft", "active"}:
            raise ValueError("不支持的产品状态")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM product_records WHERE id=%s AND status<>'deleted' FOR UPDATE",
                (product_id,),
            ).fetchone()
            if not current:
                return None
            snapshot = None
            published_media: list[dict[str, object]] = []
            search_text: str | None = None
            retired_media: list[dict[str, object]] = []
            if status == "active":
                previous_media = connection.execute(
                    """SELECT a.id::text,a.storage_path FROM product_published_media_links l
                    JOIN product_media_assets a ON a.id=l.media_id
                    WHERE l.product_id=%s""",
                    (product_id,),
                ).fetchall()
                media_rows = connection.execute(
                    """SELECT media_id::text AS id,role,sort_order
                    FROM product_media_links WHERE product_id=%s
                    ORDER BY CASE role WHEN 'product_image' THEN 0 WHEN 'body_image' THEN 1
                    WHEN 'configuration_image' THEN 2 ELSE 3 END,sort_order,media_id""",
                    (product_id,),
                ).fetchall()
                published = self._published_values(dict(current))
                published["media_ids"] = [str(item["id"]) for item in media_rows]
                snapshot = Jsonb(published)
                published_media = [dict(item) for item in media_rows]
                projected = dict(current)
                projected.update(published)
                projected["media"] = published_media
                search_text = self._build_product_search_text_from_record(projected)
            row = connection.execute(
                """UPDATE product_records SET status=%s,deleted_at=NULL,
                deleted_from_status=NULL,
                published_snapshot=CASE WHEN %s='active' THEN %s ELSE published_snapshot END,
                updated_at=now()
                WHERE id=%s AND status<>'deleted' RETURNING id""",
                (status, status, snapshot, product_id),
            ).fetchone()
            if row and status == "active":
                connection.execute(
                    "DELETE FROM product_published_media_links WHERE product_id=%s",
                    (product_id,),
                )
                if published_media:
                    with connection.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO product_published_media_links
                            (product_id,media_id,role,sort_order)
                            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            [
                                (product_id, item["id"], item["role"], item["sort_order"])
                                for item in published_media
                        ],
                    )
                for previous in previous_media:
                    if not self._media_is_referenced(connection, previous["id"]):
                        connection.execute(
                            "DELETE FROM product_media_assets WHERE id=%s", (previous["id"],)
                        )
                        retired_media.append(dict(previous))
                connection.execute(
                    """INSERT INTO product_search_documents
                    (product_id,search_text,embedding,embedding_model,embedding_status,embedding_error)
                    VALUES (%s,%s,NULL,NULL,'not_requested',NULL)
                    ON CONFLICT (product_id) DO UPDATE SET
                    search_text=excluded.search_text,embedding=NULL,embedding_model=NULL,
                    embedding_status='not_requested',embedding_error=NULL,updated_at=now()""",
                    (product_id, search_text or ""),
                )
            result = self._product_record_row(connection, product_id) if row else None
            if result and retired_media:
                result["_retired_media"] = retired_media
            return result

    def delete_product_record(self, product_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                """UPDATE product_records SET deleted_from_status=status,status='deleted',
                deleted_at=now(),updated_at=now() WHERE id=%s AND status<>'deleted'""",
                (product_id,),
            )
        return result.rowcount > 0

    def delete_product_records(self, product_ids: list[str]) -> int:
        """在一个事务中将所选产品移入回收站，不清理媒体文件。"""
        unique_ids = list(dict.fromkeys(product_ids))
        if not unique_ids:
            return 0
        if len(unique_ids) > 100:
            raise ValueError("每次最多批量删除 100 个产品")
        with self._connect() as connection:
            rows = connection.execute(
                """UPDATE product_records SET deleted_from_status=status,status='deleted',
                deleted_at=now(),updated_at=now()
                WHERE id=ANY(%s::uuid[]) AND status<>'deleted' RETURNING id::text""",
                (unique_ids,),
            ).fetchall()
        return len(rows)

    def restore_product_record(self, product_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE product_records SET status=COALESCE(deleted_from_status,'draft'),
                deleted_from_status=NULL,deleted_at=NULL,updated_at=now()
                WHERE id=%s AND status='deleted' RETURNING id""",
                (product_id,),
            ).fetchone()
            return self._product_record_row(connection, product_id) if row else None

    def permanently_delete_product_record(
        self,
        product_id: str,
        actor: str = "admin",
        *,
        expected_deleted_at: object | None = None,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            predicate = " AND deleted_at=%s" if expected_deleted_at is not None else ""
            params = (
                (product_id, expected_deleted_at)
                if expected_deleted_at is not None
                else (product_id,)
            )
            exists = connection.execute(
                f"SELECT id FROM product_records WHERE id=%s "
                f"AND status='deleted'{predicate} FOR UPDATE",
                params,
            ).fetchone()
            if not exists:
                return None
            linked = connection.execute(
                """SELECT DISTINCT a.id::text,a.storage_path FROM product_media_assets a
                JOIN (
                    SELECT media_id FROM product_media_links WHERE product_id=%s
                    UNION
                    SELECT media_id FROM product_published_media_links WHERE product_id=%s
                ) refs ON refs.media_id=a.id""",
                (product_id, product_id),
            ).fetchall()
            connection.execute(
                "DELETE FROM product_records WHERE id=%s AND status='deleted'",
                (product_id,),
            )
            connection.execute(
                """INSERT INTO business_lifecycle_audit_logs
                (id,resource_type,resource_id,action,actor)
                VALUES (%s,'product',%s,'permanent_delete',%s)""",
                (str(uuid4()), product_id, actor),
            )
            orphaned: list[dict[str, object]] = []
            for media in linked:
                referenced = self._media_is_referenced(connection, media["id"])
                if not referenced:
                    connection.execute(
                        "DELETE FROM product_media_assets WHERE id=%s", (media["id"],)
                    )
                    orphaned.append(dict(media))
        return {"media": orphaned}

    def purge_expired_product_records(self, retention_days: int = 7) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id::text,deleted_at FROM product_records WHERE status='deleted'
                AND deleted_at < now() - make_interval(days => %s)""",
                (retention_days,),
            ).fetchall()
        removable: list[dict[str, object]] = []
        for row in rows:
            result = self.permanently_delete_product_record(
                row["id"], actor="system", expected_deleted_at=row["deleted_at"]
            )
            if result:
                removable.extend(result.get("media", []))
        return removable

    def trash_stale_product_drafts(self, retention_days: int = 7) -> int:
        """将七天未编辑的产品草稿移入回收站。"""
        with self._connect() as connection:
            result = connection.execute(
                """UPDATE product_records SET deleted_from_status='draft',status='deleted',
                deleted_at=now(),updated_at=now()
                WHERE status='draft'
                  AND updated_at < now() - make_interval(days => %s)""",
                (retention_days,),
            )
        return result.rowcount

    def create_product_media(
        self,
        product_id: str,
        *,
        role: str,
        original_name: str,
        stored_path: str,
        media_type: str,
        content_hash: str,
        sort_order: int = 0,
    ) -> dict[str, object]:
        expected_type = "video" if role == "product_video" else "image"
        if role not in {"product_image", "body_image", "configuration_image", "product_video"}:
            raise ValueError("不支持的媒体角色")
        if media_type != expected_type:
            raise ValueError("媒体文件类型与角色不匹配")
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM product_records WHERE id=%s AND status<>'deleted'", (product_id,)
            ).fetchone():
                raise LookupError("产品不存在")
            asset = connection.execute(
                "SELECT id::text FROM product_media_assets WHERE content_hash=%s", (content_hash,)
            ).fetchone()
            media_id = asset["id"] if asset else str(uuid4())
            if not asset:
                connection.execute(
                    """INSERT INTO product_media_assets
                    (id,original_name,storage_path,media_type,content_hash)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (media_id, original_name, stored_path, media_type, content_hash),
                )
            displaced = []
            if role in {"product_image", "configuration_image"}:
                displaced = connection.execute(
                    """SELECT a.id::text,a.storage_path FROM product_media_assets a
                    JOIN product_media_links l ON l.media_id=a.id
                    WHERE l.product_id=%s AND l.role=%s""",
                    (product_id, role),
                ).fetchall()
                connection.execute(
                    "DELETE FROM product_media_links WHERE product_id=%s AND role=%s",
                    (product_id, role),
                )
            connection.execute(
                """INSERT INTO product_media_links (product_id,media_id,role,sort_order)
                VALUES (%s,%s,%s,%s) ON CONFLICT (product_id,media_id,role)
                DO UPDATE SET sort_order=excluded.sort_order""",
                (product_id, media_id, role, sort_order),
            )
            connection.execute(
                "UPDATE product_records SET updated_at=now() WHERE id=%s",
                (product_id,),
            )
            result = connection.execute(
                """SELECT a.id::text,a.original_name,a.storage_path,a.media_type,a.content_hash,
                l.role,l.sort_order FROM product_media_assets a JOIN product_media_links l
                ON l.media_id=a.id WHERE l.product_id=%s AND a.id=%s AND l.role=%s""",
                (product_id, media_id, role),
            ).fetchone()
            orphaned: list[dict[str, object]] = []
            for previous in displaced:
                if previous["id"] == media_id:
                    continue
                referenced = self._media_is_referenced(connection, previous["id"])
                if not referenced:
                    connection.execute(
                        "DELETE FROM product_media_assets WHERE id=%s", (previous["id"],)
                    )
                    connection.execute(
                        """INSERT INTO business_lifecycle_audit_logs
                        (id,resource_type,resource_id,action,actor)
                        VALUES (%s,'product_media',%s,'permanent_delete','admin')""",
                        (str(uuid4()), previous["id"]),
                    )
                    orphaned.append(dict(previous))
        response = dict(result)
        response["replaced_media"] = orphaned
        return response

    def list_product_media_storage_paths(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT storage_path FROM product_media_assets ORDER BY storage_path"
            ).fetchall()
        return [str(row["storage_path"]) for row in rows]

    def get_product_media(self, product_id: str, media_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.id::text,a.original_name,a.storage_path,a.media_type,a.content_hash,
                l.role,l.sort_order FROM product_media_assets a JOIN product_media_links l
                ON l.media_id=a.id WHERE l.product_id=%s AND a.id=%s""",
                (product_id, media_id),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _media_is_referenced(connection, media_id: str) -> bool:
        return bool(
            connection.execute(
                """SELECT 1 FROM product_media_links WHERE media_id=%s
                UNION ALL
                SELECT 1 FROM product_published_media_links WHERE media_id=%s
                LIMIT 1""",
                (media_id, media_id),
            ).fetchone()
        )

    def unlink_product_media(self, product_id: str, media_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            media = connection.execute(
                """SELECT a.id::text,a.storage_path FROM product_media_assets a
                JOIN product_media_links l ON l.media_id=a.id
                WHERE l.product_id=%s AND a.id=%s""",
                (product_id, media_id),
            ).fetchone()
            if not media:
                return None
            connection.execute(
                "DELETE FROM product_media_links WHERE product_id=%s AND media_id=%s",
                (product_id, media_id),
            )
            connection.execute(
                "UPDATE product_records SET updated_at=now() WHERE id=%s",
                (product_id,),
            )
            orphan = not self._media_is_referenced(connection, media_id)
            if orphan:
                connection.execute("DELETE FROM product_media_assets WHERE id=%s", (media_id,))
                connection.execute(
                    """INSERT INTO business_lifecycle_audit_logs
                    (id,resource_type,resource_id,action,actor)
                    VALUES (%s,'product_media',%s,'permanent_delete','admin')""",
                    (str(uuid4()), media_id),
                )
        result = dict(media)
        result["orphaned"] = orphan
        return result

    @classmethod
    def _build_product_search_text_from_record(cls, product: dict[str, object]) -> str:
        role_labels = {
            "product_image": "产品图",
            "body_image": "主体图",
            "configuration_image": "配置图",
            "product_video": "产品视频",
        }
        media_roles = list(
            dict.fromkeys(
                role_labels.get(str(item.get("role")), "")
                for item in product.get("media") or []
                if role_labels.get(str(item.get("role")))
            )
        )
        components = "、".join(
            "｜".join(
                part
                for part in (
                    str(item.get("name") or "").strip(),
                    f"供应商：{str(item.get('supplier') or '').strip()}"
                    if item.get("supplier")
                    else "",
                )
                if part
            )
            for item in product.get("components") or []
            if item.get("name")
        )
        def price(value: object) -> str:
            return "资料中未登记" if value is None else f"{float(value):.2f}元"

        piece_count = product.get("piece_count")
        pieces = "资料中未登记" if piece_count is None else f"{piece_count}件"
        return (
            f"产品型号：{product['model']}；系列：{product['series']}；"
            f"产品描述：{product['product_description']}；"
            f"材质：{product['material']}；档位：{product['spray_modes']}；"
            f"颜色：{product['color']}；特殊功能：{product['special_function']}；"
            f"出货地：{product['shipping_origin']}；套装价：{price(product['set_price'])}；"
            f"主体价：{price(product['body_price'])}；"
            f"件数：{pieces}；"
            f"供应商：{product['supplier']}；"
            f"主体供应商：{product['body_supplier']}；备注：{product['notes']}；"
            f"配件明细：{components}；"
            f"已关联媒体：{'、'.join(media_roles) if media_roles else '无'}；"
            "价格口径：不含税、不含运费。"
        )

    def build_product_search_text(self, product_id: str) -> str | None:
        product = self.get_active_product_record(product_id) or self.get_product_record(product_id)
        if not product:
            return None
        return self._build_product_search_text_from_record(product)

    def set_product_embedding_status(
        self, product_id: str, search_text: str, status: str, error: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO product_search_documents
                (product_id,search_text,embedding_status,embedding_error)
                VALUES (%s,%s,%s,%s) ON CONFLICT (product_id) DO UPDATE SET
                search_text=excluded.search_text,embedding_status=excluded.embedding_status,
                embedding_error=excluded.embedding_error,updated_at=now()""",
                (product_id, search_text, status, error),
            )

    def save_product_embedding(
        self, product_id: str, search_text: str, embedding: list[float], model: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO product_search_documents
                (product_id,search_text,embedding,embedding_model,embedding_status,embedding_error)
                VALUES (%s,%s,%s,%s,'ready',NULL) ON CONFLICT (product_id) DO UPDATE SET
                search_text=excluded.search_text,embedding=excluded.embedding,
                embedding_model=excluded.embedding_model,embedding_status='ready',
                embedding_error=NULL,updated_at=now()""",
                (product_id, search_text, Vector(embedding), model),
            )

    @staticmethod
    def _product_title(row: dict[str, object]) -> str:
        details = [str(row.get(key) or "") for key in ("color", "material")]
        suffix = " / ".join(item for item in details if item)
        return str(row["model"]) + (f" · {suffix}" if suffix else "")

    def search_products(
        self, query: str, limit: int = 5, query_embedding: list[float] | None = None
    ):
        from app.business.store import BusinessResult

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT p.* FROM product_records p
                WHERE p.status='active' ORDER BY p.sequence_number NULLS LAST,p.created_at"""
            ).fetchall()
        prepared = [self._active_projection(dict(row)) for row in rows]
        for row in prepared:
            active = self.get_active_product_record(str(row["id"]))
            if active:
                row.update(active)
            row["search_text"] = self._build_product_search_text_from_record(row)
        exact = [row for row in prepared if mentions_identifier(query, str(row["model"]))]
        if not exact:
            exact = [
                row
                for row in prepared
                if mentions_identifier_alias(query, str(row["model"]))
            ]
        if has_product_identifier(query) and not exact:
            return []
        if exact:
            candidates = [
                BusinessResult(
                    "product", str(row["id"]), self._product_title(row), row["search_text"], 1.0
                )
                for row in exact
            ]
            return narrow_variants(query, candidates)[:limit]
        documents = {
            str(row["id"]): f"{self._product_title(row)}\n{row['search_text']}" for row in prepared
        }
        matches = hybrid_rerank(
            query, documents, {}, limit=limit, candidate_limit=max(20, limit * 8)
        )
        by_id = {str(row["id"]): row for row in prepared}
        return [
            BusinessResult(
                "product",
                match.key,
                self._product_title(by_id[match.key]),
                documents[match.key],
                match.score,
            )
            for match in matches
        ]

    def get_active_product(self, product_id: str):
        from app.business.store import BusinessResult

        product = self.get_active_product_record(product_id)
        if not product or product["status"] != "active":
            return None
        content = self.build_product_search_text(product_id) or ""
        return BusinessResult("product", product_id, self._product_title(product), content, 1.0)

    def get_active_product_record(self, product_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            product = self._product_record_row(connection, product_id, published=True)
        if not product or product["status"] != "active":
            return None
        return self._active_projection(product)

    def list_active_products(self, limit: int = 100):
        from app.business.store import BusinessResult

        products = [
            self._active_projection(item)
            for item in self.list_product_records()
            if item["status"] == "active"
        ][:limit]
        return [
            BusinessResult(
                "product",
                item["id"],
                self._product_title(item),
                self.build_product_search_text(item["id"]) or "",
                1.0,
            )
            for item in products
        ]

    def query_products_v2(
        self,
        product_query: ProductQuery,
        *,
        include_sensitive: bool,
    ) -> ProductQueryResult:
        """Compile a validated plan into parameterized SQL over published snapshots."""
        clauses: list[str] = []
        params: list[object] = []
        if product_query.resolved_product_ids:
            # Resolved conversation references are server-side IDs.  They are
            # stricter than a model string and prevent an ordinal follow-up
            # from expanding to a same-family variant.
            placeholders = ",".join("%s" for _ in product_query.resolved_product_ids)
            clauses.append(f"id::text IN ({placeholders})")
            params.extend(product_query.resolved_product_ids)
        references = product_query.effective_references()
        if references:
            reference_clauses: list[str] = []
            for reference in references:
                if reference.kind == "context":
                    # Context references must have been resolved to an id or model before SQL.
                    if reference.value.startswith("id:"):
                        reference_clauses.append("id = %s")
                        params.append(reference.value.removeprefix("id:"))
                    else:
                        reference_clauses.append("model = %s")
                        params.append(reference.value)
                elif reference.kind == "exact":
                    reference_clauses.append("lower(model) = lower(%s)")
                    params.append(reference.value)
                else:
                    reference_clauses.append("model ILIKE %s ESCAPE E'\\\\'")
                    params.append(f"%{_escape_like(reference.value)}%")
            clauses.append("(" + " OR ".join(reference_clauses) + ")")

        for excluded in product_query.exclude_models:
            clauses.append("model NOT ILIKE %s ESCAPE E'\\\\'")
            params.append(f"%{_escape_like(excluded)}%")
        for item in product_query.filters:
            if item.field not in _QUERY_COLUMNS:
                raise ValueError(f"不支持的产品筛选字段：{item.field}")
            clauses.append(_filter_sql(item, params))

        where_sql = " AND ".join(clauses) if clauses else "TRUE"
        order_parts: list[str] = []
        for item in product_query.sorts:
            column = _QUERY_COLUMNS.get(item.field)
            if column is None or item.field in {"components", "cost_price"}:
                continue
            direction = "DESC" if item.direction == "desc" else "ASC"
            order_parts.append(f"{column} {direction} NULLS LAST")
        order_parts.extend(("sequence_number NULLS LAST", "created_at", "id"))
        order_sql = ",".join(dict.fromkeys(order_parts))

        # 分页上限是为了不把一大堆行刷给员工。但**统计类查询不返回行、只返回汇总**，
        # 它需要把匹配项全部取回来才能算对（多少种颜色、平均价、最值），
        # 所以这里对它单独放开。此前没放开的结果是：统计只覆盖了前 5 款，
        # 「一共有多少种颜色」答成 2 种（实际 12 种）——错的比不答更糟。
        if product_query.operation in {"aggregate", "count"}:
            page_cap = AGGREGATE_MAX_ROWS
        elif product_query.operation == "recommend":
            page_cap = 10
        else:
            page_cap = 5
        page_size = max(1, min(page_cap, product_query.page_size))
        page = max(1, product_query.page)
        if product_query.result_scope == "first":
            page = 1
            page_size = 1
        offset = (page - 1) * page_size
        cost_projection = "cost_price" if include_sensitive else "NULL::numeric AS cost_price"
        projection = f"""id::text,sequence_number,model,series,
        product_description,material,spray_modes,color,special_function,shipping_origin,
        set_price,{cost_projection},body_price,piece_count,supplier,body_supplier,
        components,notes,source_row,created_at,updated_at"""
        with self._connect() as connection:
            # The database enforces the same read-only boundary as the
            # fixed-column, parameterized query compiler above.
            connection.execute("SET TRANSACTION READ ONLY")
            total_row = connection.execute(
                f"SELECT count(*) AS total FROM active_product_catalog WHERE {where_sql}",
                params,
            ).fetchone()
            rows = connection.execute(
                f"""SELECT {projection} FROM active_product_catalog
                WHERE {where_sql} ORDER BY {order_sql} LIMIT %s OFFSET %s""",
                [*params, page_size, offset],
            ).fetchall()
        return ProductQueryResult(
            items=[dict(row) for row in rows],
            total=int(total_row["total"] if total_row else 0),
            page=page,
            page_size=page_size,
            has_more=page * page_size < int(total_row["total"] if total_row else 0),
        )

    def product_text_field_values(self) -> dict[str, tuple[str, ...]]:
        """已发布产品里各文本字段的**取值目录**。

        为什么需要：员工给的是"值"（"镜面玻璃·镜面"），而计划层必须决定它属于哪一列。
        模型没有取值概念，只能猜——实测同一个颜色被猜成 color、series、description 三种，
        猜错就是 0 款。有了这份目录才能把值钉到正确的字段上，也才能把
        "灰玻璃枪灰"这种漏打间隔号的写法归一到库里的标准写法。

        只取文本字段、只在**已发布**产品上取，与产品查询同一口径；
        取值总量在百级，可以整体缓存（见调用方的 TTL）。
        """
        columns = (
            "color",
            "material",
            "series",
            "product_description",
            "special_function",
            "shipping_origin",
            "spray_modes",
            "supplier",
            "body_supplier",
            "notes",
        )
        catalog: dict[str, tuple[str, ...]] = {}
        with self._connect() as connection:
            for column in columns:
                rows = connection.execute(
                    f"""SELECT {column} AS value, count(*) AS total
                    FROM active_product_catalog
                    WHERE {column} IS NOT NULL AND btrim({column}::text) <> ''
                    GROUP BY {column}
                    ORDER BY total DESC, {column}""",
                ).fetchall()
                field = "description" if column == "product_description" else column
                catalog[field] = tuple(str(row["value"]) for row in rows)
        return catalog

    def media_for_product(self, product_id: str):
        from app.business.store import BusinessResult

        product = self.get_active_product_record(product_id)
        if not product:
            return []
        return [
            BusinessResult(
                "media",
                item["id"],
                item["original_name"],
                "",
                1.0,
                item["media_type"],
                item["storage_path"],
                item["role"],
            )
            for item in product["media"]
        ]

    def search_media(
        self,
        query: str,
        media_type: str | None,
        limit: int = 5,
        related_product_id: str | None = None,
        media_role: str | None = None,
        include_all: bool = False,
    ):
        if related_product_id:
            items = self.media_for_product(related_product_id)
        else:
            items = []
            for product in self.list_product_records():
                if product["status"] == "active" and (
                    include_all or mentions_identifier(query, str(product["model"]))
                ):
                    items.extend(self.media_for_product(product["id"]))
        if media_type:
            items = [item for item in items if item.media_type == media_type]
        if media_role:
            items = [item for item in items if item.media_role == media_role]
        return items[:limit]

    def single_active_product_id(self) -> str | None:
        active = [item["id"] for item in self.list_product_records() if item["status"] == "active"]
        return active[0] if len(active) == 1 else None
