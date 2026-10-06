import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.agent import get_rag_agent
from app.config import get_settings
from app.knowledge.file_cleanup import delete_document_files
from app.knowledge.processor import SUPPORTED_EXTENSIONS
from app.knowledge.service import get_knowledge_store
from app.knowledge.store import (
    DocumentNotFoundError,
    DocumentNotReadyError,
    DuplicateDocumentError,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/settings")
async def knowledge_settings() -> dict[str, int]:
    settings = get_settings()
    return {
        "trash_retention_days": settings.business_trash_retention_days,
        "draft_retention_days": settings.business_draft_retention_days,
    }


class DocumentMetadata(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)

    @field_validator("title", "category")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class BulkDocumentDelete(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=100)


async def _index_safely(document_id: str) -> None:
    try:
        await asyncio.to_thread(get_rag_agent().index_document, document_id)
    except Exception:
        # Index status and error are persisted by RagAgent; publishing remains complete.
        return


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    category: Annotated[str, Form()] = "其他",
    document_kind: Annotated[str, Form()] = "policy",
) -> dict[str, object]:
    settings = get_settings()
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="首版仅支持 TXT、Markdown 和 PDF")
    normalized_category = (
        "检测报告"
        if document_kind.strip().lower() == "report"
        else category.strip() or "其他"
    )
    if normalized_category == "检测报告" and extension != ".pdf":
        raise HTTPException(status_code=415, detail="检测报告目前只允许上传 PDF")

    max_bytes = settings.knowledge_max_upload_mb * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"单个文件暂时不能超过 {settings.knowledge_max_upload_mb}MB",
        )
    if not content:
        raise HTTPException(status_code=400, detail="不能上传空文件")
    try:
        document = await asyncio.to_thread(
            get_knowledge_store().ingest,
            title=title.strip() or filename,
            original_name=filename,
            category=normalized_category,
            content=content,
        )
    except DuplicateDocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(document)


@router.get("/documents")
async def list_documents(
    include_deleted: bool = Query(default=False),
    category: str | None = Query(default=None, max_length=100),
) -> list[dict[str, object]]:
    documents = await asyncio.to_thread(
        get_knowledge_store().list_documents, include_deleted=include_deleted
    )
    return [
        asdict(document)
        for document in documents
        if not category or document.category == category
    ]


@router.patch("/documents/{document_id}")
async def update_document(document_id: str, payload: DocumentMetadata) -> dict[str, object]:
    try:
        document = await asyncio.to_thread(
            get_knowledge_store().update_metadata,
            document_id,
            payload.title,
            payload.category,
        )
        return asdict(document)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/documents/{document_id}/replacement", status_code=status.HTTP_201_CREATED)
async def upload_document_replacement(
    document_id: str,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
) -> dict[str, object]:
    filename = file.filename or ""
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="仅支持 TXT、Markdown 和 PDF")
    settings = get_settings()
    current = None
    getter = getattr(get_knowledge_store(), "get_document", None)
    if callable(getter):
        try:
            current = await asyncio.to_thread(getter, document_id)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if (
        current is not None
        and current.category == "检测报告"
        and Path(filename).suffix.lower() != ".pdf"
    ):
        raise HTTPException(status_code=415, detail="检测报告目前只允许上传 PDF")
    content = await file.read(settings.knowledge_max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.knowledge_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
    if not content:
        raise HTTPException(status_code=400, detail="不能上传空文件")
    try:
        document = await asyncio.to_thread(
            get_knowledge_store().prepare_replacement,
            document_id,
            title=title.strip(),
            original_name=filename,
            category=category.strip() or (current.category if current is not None else ""),
            content=content,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateDocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(document)


@router.get("/documents/{document_id}/chunks")
async def preview_document_text(
    document_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, object]]:
    try:
        if offset == 0 and limit == 100:
            return await asyncio.to_thread(get_knowledge_store().document_chunks, document_id)
        return await asyncio.to_thread(
            get_knowledge_store().document_chunks, document_id, offset=offset, limit=limit
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/documents/{document_id}/file")
async def get_document_file(document_id: str) -> FileResponse:
    try:
        document, path = await asyncio.gather(
            asyncio.to_thread(get_knowledge_store().get_document, document_id),
            asyncio.to_thread(get_knowledge_store().stored_path, document_id),
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    upload_dir = Path(get_settings().knowledge_upload_dir).resolve()
    try:
        path.resolve().relative_to(upload_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="原文件路径无效") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="原文件不存在")
    return FileResponse(path, filename=document.original_name, content_disposition_type="inline")


@router.post("/documents/{document_id}/publish")
async def publish_document(document_id: str) -> dict[str, object]:
    try:
        document = await asyncio.to_thread(get_knowledge_store().publish, document_id)
        if document.category != "检测报告":
            asyncio.create_task(_index_safely(document_id))
        return asdict(document)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/documents/{document_id}/embedding/retry")
async def retry_document_embedding(document_id: str) -> dict[str, object]:
    store = get_knowledge_store()
    try:
        document = await asyncio.to_thread(store.get_document, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if document.status != "published":
        raise HTTPException(status_code=409, detail="只有已发布资料可以重新生成检索向量")
    if document.category == "检测报告":
        raise HTTPException(status_code=409, detail="检测报告只按标题或文件名匹配，不生成正文向量")
    try:
        indexed = await asyncio.to_thread(get_rag_agent().index_document, document_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="向量生成失败，请稍后重试") from exc
    refreshed = await asyncio.to_thread(store.get_document, document_id)
    return {"document": asdict(refreshed), "indexed_chunks": indexed}


@router.post("/documents/{document_id}/reprocess")
async def reprocess_document(document_id: str) -> dict[str, object]:
    """Reparse a stored source and rebuild its chunks without changing publication state."""
    store = get_knowledge_store()
    try:
        current = await asyncio.to_thread(store.get_document, document_id)
        if current.category == "检测报告":
            raise HTTPException(status_code=409, detail="检测报告不解析正文")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        _, parsed_chunks = await asyncio.to_thread(store.reprocess_document, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        indexed_chunks = await asyncio.to_thread(get_rag_agent().index_document, document_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="文档已重新解析，但向量生成失败；请使用向量重试功能",
        ) from exc
    refreshed = await asyncio.to_thread(store.get_document, document_id)
    return {
        "document": asdict(refreshed),
        "parsed_chunks": parsed_chunks,
        "indexed_chunks": indexed_chunks,
    }


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str) -> None:
    deleted = await asyncio.to_thread(get_knowledge_store().delete_document, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="资料不存在")


@router.post("/documents/bulk-trash")
async def bulk_delete_documents(payload: BulkDocumentDelete) -> dict[str, int]:
    document_ids = [str(document_id) for document_id in payload.document_ids]
    deleted = await asyncio.to_thread(get_knowledge_store().delete_documents, document_ids)
    if not deleted:
        raise HTTPException(status_code=404, detail="所选制度不存在或已在回收站")
    return {"deleted": deleted}


@router.post("/documents/{document_id}/restore")
async def restore_document(document_id: str) -> dict[str, object]:
    document = await asyncio.to_thread(get_knowledge_store().restore_document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="回收站中没有这个资料")
    return asdict(document)


@router.delete("/documents/{document_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def permanently_delete_document(document_id: str) -> None:
    store = get_knowledge_store()
    paths = await asyncio.to_thread(store.permanently_delete_document, document_id)
    if paths is None:
        raise HTTPException(status_code=404, detail="回收站中没有这个资料")
    await asyncio.to_thread(delete_document_files, paths, get_settings().knowledge_upload_dir)


@router.get("/search")
async def search_knowledge(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=5, ge=1, le=20),
) -> list[dict[str, object]]:
    results = await asyncio.to_thread(get_rag_agent().search, q, limit)
    return [asdict(result) for result in results]
