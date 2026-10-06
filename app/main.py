import asyncio
import base64
import binascii
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

import psycopg
import redis
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import STATIC_DIR
from app.admin.router import router as admin_router
from app.business.media_files import delete_uploaded_media_files
from app.business.router import router as business_router
from app.business.service import get_business_agent, get_business_store
from app.config import get_settings
from app.knowledge.file_cleanup import delete_document_files
from app.knowledge.router import router as knowledge_router
from app.knowledge.service import get_knowledge_store
from app.knowledge.store import psycopg_url
from app.logging import configure_logging
from app.wecom.health import read_wecom_connection_status
from app.wecom.router import router as wecom_router

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger()


async def cleanup_product_trash() -> None:
    try:
        stale_scripts = await asyncio.to_thread(
            get_business_store().trash_stale_reply_script_drafts,
            settings.business_draft_retention_days,
        )
        stale_products = await asyncio.to_thread(
            get_business_store().trash_stale_product_drafts,
            settings.business_draft_retention_days,
        )
        stale_documents = await asyncio.to_thread(
            get_knowledge_store().trash_stale_drafts,
            settings.business_draft_retention_days,
        )
        if stale_scripts or stale_products or stale_documents:
            await logger.ainfo(
                "stale_drafts_moved_to_trash",
                reply_scripts=stale_scripts,
                products=stale_products,
                documents=stale_documents,
            )
        expired_scripts = await asyncio.to_thread(
            get_business_store().purge_expired_reply_scripts,
            settings.business_trash_retention_days,
        )
        if expired_scripts:
            await logger.ainfo("reply_script_trash_cleanup_completed", records=expired_scripts)
        expired_media = await asyncio.to_thread(
            get_business_store().purge_expired_product_records,
            settings.business_trash_retention_days,
        )
        deleted_files = await asyncio.to_thread(
            delete_uploaded_media_files,
            expired_media,
            settings.business_media_upload_dir,
        )
        if expired_media or deleted_files:
            await logger.ainfo(
                "product_trash_cleanup_completed",
                media_records=len(expired_media),
                files=deleted_files,
            )
        expired_documents = await asyncio.to_thread(
            get_knowledge_store().purge_expired_documents,
            settings.business_trash_retention_days,
        )
        deleted_document_files = await asyncio.to_thread(
            delete_document_files, expired_documents, settings.knowledge_upload_dir
        )
        if expired_documents or deleted_document_files:
            await logger.ainfo(
                "knowledge_trash_cleanup_completed",
                documents=len(expired_documents),
                files=deleted_document_files,
            )
    except Exception as exc:
        await logger.aerror("product_trash_cleanup_failed", error_type=type(exc).__name__)


async def product_trash_cleanup_loop() -> None:
    while True:
        await cleanup_product_trash()
        await asyncio.sleep(24 * 60 * 60)


async def bootstrap_search_indexes() -> None:
    try:
        indexed = await asyncio.to_thread(get_business_agent().index_published_reply_scripts)
        if indexed:
            await logger.ainfo("reply_script_index_bootstrap_completed", indexed=indexed)
    except Exception as exc:
        await logger.aerror("reply_script_index_bootstrap_failed", error_type=type(exc).__name__)


async def bootstrap_langgraph_checkpoint() -> None:
    if not getattr(settings, "langgraph_checkpoint_enabled", False):
        return
    await asyncio.to_thread(get_business_agent()._get_business_graph)
    await logger.ainfo("langgraph_checkpoint_initialized")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await logger.ainfo("application_started", environment=settings.app_env)
    await bootstrap_langgraph_checkpoint()
    trash_cleanup_task = asyncio.create_task(product_trash_cleanup_loop())
    search_index_task = asyncio.create_task(bootstrap_search_indexes())
    try:
        yield
    finally:
        for task in (trash_cleanup_task, search_index_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if getattr(settings, "langgraph_checkpoint_enabled", False):
            # Tests and lightweight health clients may disable the bootstrap
            # hook; do not instantiate the database-backed agent merely to
            # close an agent that was never created.
            cached_agent = get_business_agent.cache_info().currsize
            if cached_agent:
                await asyncio.to_thread(get_business_agent().close)
        await logger.ainfo("application_stopped")


app = FastAPI(
    title="企业微信智能知识库与产品报价机器人",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def admin_authentication(request: Request, call_next):
    """生产环境同时支持浏览器 Basic 登录和自动化 X-Admin-Key。"""
    protected = request.url.path == "/admin" or request.url.path.startswith(
        ("/admin/", "/knowledge", "/business", "/docs", "/redoc", "/openapi.json")
    )
    if settings.app_env == "production" and protected:
        configured = settings.admin_api_key
        supplied = request.headers.get("X-Admin-Key", "")
        authorization = request.headers.get("Authorization", "")
        # 只有通过验证的 API key 才能豁免浏览器来源检查。
        valid_api_key = bool(
            configured
            and supplied
            and secrets.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8"))
        )
        browser_basic = authorization.startswith("Basic ") and not valid_api_key
        if browser_basic and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            referer = request.headers.get("Referer")
            source = origin or referer
            parsed = urlparse(source or "")
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or (
                source is not None
                and (parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc)
            ):
                return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        if authorization.startswith("Basic ") and not valid_api_key:
            try:
                decoded = base64.b64decode(
                    authorization.removeprefix("Basic "), validate=True
                ).decode("utf-8")
                username, _, password = decoded.partition(":")
                if username == "admin":
                    supplied = password
            except (binascii.Error, UnicodeDecodeError):
                supplied = ""
        if not configured or not secrets.compare_digest(
            supplied.encode("utf-8"), configured.encode("utf-8")
        ):
            return JSONResponse(
                {"detail": "管理接口未授权"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Knowledge Admin"'},
            )
    return await call_next(request)


app.include_router(wecom_router)
app.include_router(knowledge_router)
app.include_router(business_router)
app.include_router(admin_router)
app.mount("/admin-static", StaticFiles(directory=STATIC_DIR), name="admin-static")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
    }


def check_dependencies() -> dict[str, str]:
    """执行有超时的依赖探测，不返回连接地址或密钥。"""
    checks: dict[str, str] = {}
    if not settings.database_url:
        checks["postgresql"] = "not_configured"
    else:
        try:
            with psycopg.connect(
                psycopg_url(settings.database_url), connect_timeout=2
            ) as connection:
                connection.execute("SELECT 1")
                required = connection.execute(
                    """SELECT extname FROM pg_extension
                    WHERE extname IN ('vector', 'pg_trgm')"""
                ).fetchall()
                if {row["extname"] if isinstance(row, dict) else row[0] for row in required} != {
                    "vector",
                    "pg_trgm",
                }:
                    checks["postgresql"] = "schema_incomplete"
                else:
                    required_tables = (
                        "knowledge_documents",
                        "knowledge_chunks",
                        "product_records",
                        "product_search_documents",
                        "product_media_assets",
                        "product_media_links",
                        "product_published_media_links",
                        "reply_scripts",
                        "agent_messages",
                        "wecom_message_dedup",
                    )
                    if getattr(settings, "langgraph_checkpoint_enabled", False):
                        required_tables += (
                            "checkpoint_migrations",
                            "checkpoints",
                            "checkpoint_blobs",
                            "checkpoint_writes",
                        )
                    table_rows = connection.execute(
                        "SELECT name, to_regclass(name) FROM unnest(%s::text[]) AS name",
                        (list(required_tables),),
                    ).fetchall()
                    vector_rows = connection.execute(
                        """SELECT format_type(a.atttypid,a.atttypmod)
                        FROM pg_attribute a
                        WHERE a.attrelid=to_regclass('knowledge_chunks')
                        AND a.attname='embedding' AND NOT a.attisdropped"""
                    ).fetchall()
                    schema_ok = (
                        all(row[1] is not None for row in table_rows)
                        and bool(vector_rows)
                        and vector_rows[0][0] == f"vector({settings.embedding_dimensions})"
                    )
                    checks["postgresql"] = "ok" if schema_ok else "schema_incomplete"
        except Exception:
            checks["postgresql"] = "unavailable"

    if not settings.redis_url:
        checks["redis"] = "not_configured"
    else:
        try:
            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            client.close()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"

    checks["llm"] = (
        "configured"
        if settings.llm_api_key and settings.llm_model and settings.llm_base_url
        else "not_configured"
    )
    checks["embedding"] = (
        "configured"
        if settings.embedding_api_key and settings.embedding_model and settings.embedding_base_url
        else "not_configured"
    )
    checks["wecom"] = read_wecom_connection_status(settings)
    checks["wecom_directory"] = (
        "configured"
        if settings.wecom_corp_id
        and settings.wecom_secret
        and settings.wecom_sales_department_id is not None
        else "not_configured"
    )
    return checks


@app.get("/health/ready", tags=["system"])
async def readiness() -> JSONResponse:
    checks = await asyncio.to_thread(check_dependencies)
    ready = (
        checks.get("postgresql") == "ok"
        and checks.get("redis") in {"ok", "not_configured"}
        and checks.get("llm") == "configured"
        and checks.get("embedding") == "configured"
        and checks.get("wecom") in {"online", "not_configured"}
    )
    return JSONResponse(
        {
            "status": "ready" if ready else "degraded",
            "service": settings.app_name,
            "dependencies": checks,
        },
        status_code=200 if ready else 503,
    )
