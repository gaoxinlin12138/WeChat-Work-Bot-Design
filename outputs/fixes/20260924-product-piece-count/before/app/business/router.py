import asyncio
import hashlib
import json
import logging
import mimetypes
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from app.agent import get_rag_agent
from app.business.intent import Intent, ReplyScenario, classify_reply_scenario
from app.business.media_files import delete_uploaded_media_files
from app.business.media_policy import (
    WECOM_IMAGE_SAFE_MAX_BYTES,
    WECOM_IMAGE_SAFE_MAX_PIXELS,
    WECOM_VIDEO_OVERSIZE_MESSAGE,
    WECOM_VIDEO_SAFE_MAX_BYTES,
)
from app.business.product_import import parse_product_workbook
from app.business.script_import import (
    ImportLimitError,
    parse_scripts,
    parse_xlsx_cases,
    validate_import_size,
    validation_warnings,
)
from app.business.service import get_business_agent, get_business_store
from app.config import get_settings

router = APIRouter(prefix="/business", tags=["business"])
logger = logging.getLogger(__name__)


def _classify_case(question: str, answer: str) -> dict[str, object]:
    """优先用 DeepSeek 归类；服务不可用时使用可解释的本地兜底。"""
    scenario = classify_reply_scenario(question)
    scenario_label = {
        ReplyScenario.PRODUCT_INQUIRY: "产品咨询",
        ReplyScenario.PRICE_NEGOTIATION: "报价议价",
        ReplyScenario.AFTER_SALES_COMPLAINT: "售后投诉",
        ReplyScenario.DELIVERY_DELAY: "交付延期",
        ReplyScenario.CUSTOMER_FOLLOW_UP: "客户跟进",
    }.get(scenario, "未分类")
    emotion = (
        "不满"
        if any(word in question for word in ("投诉", "生气", "不满", "太差"))
        else (
            "着急" if any(word in question for word in ("催", "尽快", "急", "什么时候")) else "平和"
        )
    )
    intent = scenario_label
    quality_score = 0.6 if len(answer) > 180 else 0.8
    settings = get_settings()
    if settings.llm_api_key and settings.llm_model:
        try:
            response = (
                get_rag_agent()
                ._llm_client()
                .chat.completions.create(
                    model=settings.llm_model,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "请把客户问答归类为 JSON，只返回 scenario、intent、emotion、"
                                "quality_score。scenario 只能是：产品咨询、报价议价、售后投诉、"
                                "交付延期、客户跟进、未分类；emotion 只能是：平和、犹豫、着急、"
                                "不满。"
                                "quality_score 为0到1之间的数字。"
                            ),
                        },
                        {"role": "user", "content": f"客户问题：{question}\n员工回复：{answer}"},
                    ],
                )
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
            if parsed.get("scenario"):
                scenario_label = str(parsed["scenario"])
            if parsed.get("intent"):
                intent = str(parsed["intent"])
            if parsed.get("emotion"):
                emotion = str(parsed["emotion"])
            quality_score = max(0.0, min(1.0, float(parsed.get("quality_score", quality_score))))
        except Exception as exc:
            logger.warning("Case classification fell back: %s", type(exc).__name__)
    return {
        "scenario": scenario_label,
        "intent": intent,
        "emotion": emotion,
        "quality_score": quality_score,
    }


class ScriptCreate(BaseModel):
    title: str = Field(default="客户问答案例", min_length=1, max_length=300)
    scenario: str = Field(default="未分类", min_length=1, max_length=200)
    audience: Literal["customer"] = "customer"
    content: str = Field(min_length=1)
    customer_question: str = Field(default="", max_length=2000)
    conditions: str = Field(default="", max_length=2000)
    required_fields: list[str] = Field(default_factory=list, max_length=30)
    forbidden_phrases: list[str] = Field(default_factory=list, max_length=50)
    variables: list[str] = Field(default_factory=list, max_length=30)
    product_scope: str = Field(default="", max_length=500)

    @field_validator("required_fields", "forbidden_phrases", "variables")
    @classmethod
    def clean_script_terms(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class ScriptEdit(BaseModel):
    customer_question: str = Field(min_length=1, max_length=2000)
    employee_reply: str = Field(min_length=1, max_length=10000)

    @field_validator("customer_question", "employee_reply")
    @classmethod
    def strip_script_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class BulkScriptDelete(BaseModel):
    script_ids: list[UUID] = Field(min_length=1, max_length=100)


class BulkProductDelete(BaseModel):
    product_ids: list[UUID] = Field(min_length=1, max_length=100)


class IntentAuditCorrection(BaseModel):
    corrected_intent: Intent | None = None
    reason: str = Field(default="", max_length=500)
    corrected_by: str = Field(default="admin", min_length=1, max_length=100)


class ProductRecordUpsert(BaseModel):
    sequence_number: int | None = Field(default=None, ge=0)
    model: str = Field(min_length=1, max_length=200)
    series: str = Field(default="", max_length=200)
    product_description: str = Field(default="", max_length=5000)
    style: str = Field(default="", max_length=200)
    material: str = Field(default="", max_length=200)
    spray_modes: str = Field(default="", max_length=100)
    color: str = Field(default="", max_length=200)
    special_function: str = Field(default="", max_length=2000)
    shipping_origin: str = Field(default="", max_length=200)
    set_price: Decimal | None = Field(default=None, ge=0, le=9999999999, decimal_places=2)
    body_price: Decimal | None = Field(default=None, ge=0, le=9999999999, decimal_places=2)
    cost_price: Decimal | None = Field(default=None, ge=0, le=9999999999, decimal_places=2)
    supplier: str = Field(default="", max_length=300)
    body_supplier: str = Field(default="", max_length=300)
    components: list[dict[str, object]] = Field(default_factory=list, max_length=100)
    notes: str = Field(default="", max_length=5000)
    source_row: int | None = Field(default=None, ge=2)

    @field_validator(
        "model",
        "series",
        "product_description",
        "style",
        "material",
        "spray_modes",
        "color",
        "special_function",
        "shipping_origin",
        "supplier",
        "body_supplier",
        "notes",
    )
    @classmethod
    def strip_product_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("components")
    @classmethod
    def clean_components(cls, values: list[dict[str, object]]) -> list[dict[str, object]]:
        cleaned: list[dict[str, object]] = []
        for item in values:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            supplier = str(item.get("supplier") or "").strip()
            unit_price = item.get("unit_price")
            if unit_price in ("", None):
                unit_price = None
            elif isinstance(unit_price, str):
                try:
                    unit_price = Decimal(unit_price)
                except Exception as exc:
                    raise ValueError(f"配件“{name}”的采购价无效") from exc
            if unit_price is not None and Decimal(str(unit_price)) < 0:
                raise ValueError(f"配件“{name}”的采购价不能小于 0")
            cleaned.append({"name": name, "supplier": supplier, "unit_price": unit_price})
        return cleaned


def _valid_video_container(extension: str, content: bytes) -> bool:
    """Perform a small signature check so renamed arbitrary files are rejected.

    MP4/M4V/MOV all use an ISO Base Media File Format ``ftyp`` box near the
    beginning of the file. WebM uses the EBML magic number. This is deliberately
    a lightweight check; full codec validation belongs to an offline media
    processor and is not needed for upload admission.
    """
    if extension in {".mp4", ".m4v", ".mov"}:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if extension == ".webm":
        return content.startswith(b"\x1a\x45\xdf\xa3")
    return False


@router.post("/scripts")
def create_script(payload: ScriptCreate) -> dict[str, object]:
    values = payload.model_dump()
    if values["title"] == "客户问答案例" and values.get("customer_question"):
        values["title"] = "客户问答案例：" + values["customer_question"][:30]
    labels = _classify_case(values.get("customer_question", ""), values["content"])
    if values["scenario"] == "未分类":
        values["scenario"] = labels["scenario"]
    values.update(labels)
    values["employee_reply"] = values["content"]
    values["validation_warnings"] = validation_warnings(values)
    return get_business_store().create_script(**values)


@router.post("/scripts/upload")
async def upload_scripts(
    file: Annotated[UploadFile, File(...)],
    default_scenario: Annotated[str, Form()] = "未分类",
) -> dict[str, object]:
    filename = file.filename or "reply-scripts.txt"
    settings = get_settings()
    max_bytes = settings.business_script_max_upload_mb * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="话术文件超过大小限制")
    return await asyncio.to_thread(_import_scripts, filename, content, default_scenario)


def _import_scripts(filename: str, content: bytes, default_scenario: str) -> dict[str, object]:
    """Parsing, model calls and database writes must not block the ASGI event loop."""
    try:
        scripts = (
            parse_xlsx_cases(content)
            if Path(filename).suffix.lower() == ".xlsx"
            else parse_scripts(filename, content, default_scenario)
        )
    except ImportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"话术文件解析失败：{exc}") from exc
    if not scripts:
        raise HTTPException(status_code=422, detail="文件中没有解析出有效话术")
    try:
        validate_import_size(scripts)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    store = get_business_store()
    created = []
    for item in scripts:
        question = str(item.get("customer_question") or "")
        answer = str(item.get("employee_reply") or item.get("content") or "")
        labels = _classify_case(question or str(item.get("title")), answer)
        item.update(labels)
        item["content"] = answer
        item["scenario"] = str(item.get("scenario") or default_scenario)
        created.append(
            store.create_script(
                title=str(item["title"]),
                scenario=str(item["scenario"]),
                audience="customer",
                content=answer,
                conditions=str(item.get("conditions") or ""),
                required_fields=list(item.get("required_fields") or []),
                forbidden_phrases=list(item.get("forbidden_phrases") or []),
                variables=list(item.get("variables") or []),
                product_scope=str(item.get("product_scope") or ""),
                validation_warnings=validation_warnings(item),
                customer_question=question,
                employee_reply=answer,
                intent=str(item.get("intent") or ""),
                emotion=str(item.get("emotion") or ""),
                quality_score=float(item.get("quality_score") or 0),
                source_row=int(item["source_row"]) if item.get("source_row") else None,
            )
        )
    return {"count": len(created), "scripts": created}


@router.get("/scripts")
def list_scripts(include_deleted: bool = False) -> list[dict[str, object]]:
    return get_business_store().list_reply_scripts(include_deleted=include_deleted)


@router.post("/scripts/bulk-trash")
def bulk_delete_scripts(payload: BulkScriptDelete) -> dict[str, int]:
    deleted = get_business_store().delete_scripts([str(item) for item in payload.script_ids])
    return {"deleted": deleted}


@router.put("/scripts/{script_id}")
def edit_script(script_id: str, payload: ScriptEdit) -> dict[str, object]:
    question = payload.customer_question
    answer = payload.employee_reply
    values: dict[str, object] = {
        "customer_question": question,
        "employee_reply": answer,
        "content": answer,
    }
    values.update(_classify_case(question, answer))
    values["validation_warnings"] = validation_warnings(values)
    result = get_business_store().save_script_edit(script_id, values)
    if not result:
        raise HTTPException(status_code=404, detail="话术不存在")
    return result


@router.post("/scripts/{script_id}/publish")
def publish_script(script_id: str) -> dict[str, object]:
    result = get_business_store().publish_script(script_id)
    if not result:
        raise HTTPException(status_code=404, detail="话术不存在")
    try:
        get_business_agent().index_published_reply_scripts()
    except Exception:
        # 话术仍然发布成功，向量不可用时检索会自动回退到中文 BM25。
        pass
    return result


@router.delete("/scripts/{script_id}", status_code=204)
def delete_script(script_id: str) -> None:
    result = get_business_store().delete_script(script_id)
    if not result:
        raise HTTPException(status_code=404, detail="话术不存在")


@router.post("/scripts/{script_id}/restore")
def restore_script(script_id: str) -> dict[str, object]:
    result = get_business_store().restore_script(script_id)
    if not result:
        raise HTTPException(status_code=404, detail="回收站中没有这条话术")
    return result


@router.delete("/scripts/{script_id}/permanent", status_code=204)
def permanently_delete_script(script_id: str) -> None:
    if not get_business_store().permanently_delete_script(script_id):
        raise HTTPException(status_code=404, detail="回收站中没有这条话术")


@router.post("/scripts/{script_id}/versions")
def create_script_version(script_id: str, payload: ScriptCreate) -> dict[str, object]:
    values = payload.model_dump()
    values["validation_warnings"] = validation_warnings(values)
    result = get_business_store().create_script_version(script_id, values)
    if not result:
        raise HTTPException(status_code=404, detail="话术不存在")
    return result


@router.post("/products/import-xlsx")
async def import_products_xlsx(
    file: Annotated[UploadFile, File(...)],
) -> dict[str, object]:
    filename = file.filename or ""
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=415, detail="产品档案仅支持 .xlsx 文件")
    content = await file.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="产品档案 Excel 不能超过 20MB")
    try:
        parsed = await asyncio.to_thread(parse_product_workbook, content)
        created = await asyncio.to_thread(
            get_business_store().import_product_records, parsed["records"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"count": len(created), "products": created, "warnings": parsed["warnings"]}


@router.post("/products")
def create_product(payload: ProductRecordUpsert) -> dict[str, object]:
    return get_business_store().create_product_record(payload.model_dump())


@router.get("/products")
def list_products(include_deleted: bool = False) -> list[dict[str, object]]:
    return get_business_store().list_product_records(include_deleted=include_deleted)


@router.post("/products/bulk-trash")
def bulk_delete_products(payload: BulkProductDelete) -> dict[str, int]:
    deleted = get_business_store().delete_product_records(
        [str(item) for item in payload.product_ids]
    )
    return {"deleted": deleted}


@router.get("/products/{product_id}")
def get_product(product_id: str) -> dict[str, object]:
    product = get_business_store().get_product_record(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")
    return product


@router.put("/products/{product_id}")
def update_product(product_id: str, payload: ProductRecordUpsert) -> dict[str, object]:
    product = get_business_store().update_product_record(product_id, payload.model_dump())
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")
    return product


@router.post("/products/{product_id}/publish")
def publish_product(product_id: str) -> dict[str, object]:
    product = get_business_store().set_product_status(product_id, "active")
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")
    retired_media = list(product.pop("_retired_media", []))
    try:
        get_business_agent().index_product_record(product_id)
    except Exception:
        pass
    if retired_media:
        delete_uploaded_media_files(retired_media, get_settings().business_media_upload_dir)
    return product


@router.post("/products/{product_id}/disable")
def disable_product(product_id: str) -> dict[str, object]:
    product = get_business_store().set_product_status(product_id, "draft")
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")
    return product


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str) -> None:
    if not get_business_store().delete_product_record(product_id):
        raise HTTPException(status_code=404, detail="产品不存在")


@router.post("/products/{product_id}/restore")
def restore_product(product_id: str) -> dict[str, object]:
    product = get_business_store().restore_product_record(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="回收站中没有这个产品")
    return product


@router.delete("/products/{product_id}/permanent", status_code=204)
def permanently_delete_product(product_id: str) -> None:
    result = get_business_store().permanently_delete_product_record(product_id)
    if not result:
        raise HTTPException(status_code=404, detail="回收站中没有这个产品")
    delete_uploaded_media_files(result.get("media", []), get_settings().business_media_upload_dir)


@router.post("/products/{product_id}/media")
async def upload_product_media(
    product_id: str,
    file: Annotated[UploadFile, File(...)],
    role: Annotated[
        Literal["main_image", "gallery_image", "configuration_image", "product_video"],
        Form(),
    ],
    sort_order: Annotated[int, Form()] = 0,
) -> dict[str, object]:
    settings = get_settings()
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    video_extensions = {".mp4"}
    expected = video_extensions if role == "product_video" else image_extensions
    if extension not in expected:
        detail = "产品视频仅支持MP4文件" if role == "product_video" else "文件类型与媒体角色不匹配"
        raise HTTPException(status_code=415, detail=detail)
    content = await file.read(settings.business_media_max_upload_mb * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(status_code=400, detail="不能上传空文件")
    if len(content) > settings.business_media_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="媒体文件超过大小限制")
    if role == "product_video" and len(content) > WECOM_VIDEO_SAFE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=WECOM_VIDEO_OVERSIZE_MESSAGE,
        )
    media_type = "video" if role == "product_video" else "image"
    if media_type == "image":
        if len(content) > WECOM_IMAGE_SAFE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="图片文件不能超过 10MB")
        try:
            with Image.open(BytesIO(content)) as image:
                too_many_pixels = image.width * image.height > WECOM_IMAGE_SAFE_MAX_PIXELS
                image.verify()
            if too_many_pixels:
                raise HTTPException(status_code=413, detail="图片像素不能超过 2500 万")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=415, detail="图片文件内容无效") from exc
    elif not _valid_video_container(extension, content):
        raise HTTPException(status_code=415, detail="视频文件内容无效")
    upload_dir = Path(settings.business_media_upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(content).hexdigest()
    saved_path = upload_dir / f"{uuid4()}{extension}"
    try:
        saved_path.write_bytes(content)
        media = get_business_store().create_product_media(
            product_id,
            role=role,
            original_name=filename,
            stored_path=str(saved_path),
            media_type=media_type,
            content_hash=content_hash,
            sort_order=sort_order,
        )
    except LookupError as exc:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if Path(str(media["storage_path"])).resolve() != saved_path:
        saved_path.unlink(missing_ok=True)
    replaced_media = list(media.pop("replaced_media", []))
    if replaced_media:
        delete_uploaded_media_files(replaced_media, settings.business_media_upload_dir)
    return media


@router.get("/products/{product_id}/media/{media_id}/file")
def get_product_media_file(product_id: str, media_id: str) -> FileResponse:
    media = get_business_store().get_product_media(product_id, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="产品媒体不存在")
    upload_dir = Path(get_settings().business_media_upload_dir).resolve()
    path = Path(str(media["storage_path"])).resolve()
    try:
        path.relative_to(upload_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="媒体文件路径无效") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    media_type = mimetypes.guess_type(str(media["original_name"]))[0] or (
        "video/mp4" if media.get("media_type") == "video" else "application/octet-stream"
    )
    # 预览缩略图和视频需要浏览器内联渲染；保留原文件名供新窗口打开时识别。
    filename = quote(str(media["original_name"]).replace('"', ""), safe="")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"},
    )


@router.delete("/products/{product_id}/media/{media_id}", status_code=204)
def delete_product_media(product_id: str, media_id: str) -> None:
    media = get_business_store().unlink_product_media(product_id, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="产品媒体不存在")
    if media.get("orphaned"):
        delete_uploaded_media_files([media], get_settings().business_media_upload_dir)


@router.get("/intent-audits")
def list_intent_audits(
    limit: int = 50,
    offset: int = 0,
    intent: Intent | None = None,
    corrected: bool | None = None,
    min_confidence: float | None = None,
) -> list[dict[str, object]]:
    return get_business_store().list_intent_audits(
        limit=limit,
        offset=offset,
        intent=str(intent) if intent else None,
        corrected=corrected,
        min_confidence=min_confidence,
    )


@router.get("/intent-audits/{audit_id}")
def get_intent_audit(audit_id: str) -> dict[str, object]:
    result = get_business_store().get_intent_audit(audit_id)
    if not result:
        raise HTTPException(status_code=404, detail="识别记录不存在")
    return result


@router.post("/intent-audits/{audit_id}/correction")
def correct_intent_audit(audit_id: str, payload: IntentAuditCorrection) -> dict[str, object]:
    result = get_business_store().correct_intent_audit(
        audit_id,
        str(payload.corrected_intent) if payload.corrected_intent else None,
        payload.reason,
        payload.corrected_by,
    )
    if not result:
        raise HTTPException(status_code=404, detail="识别记录不存在")
    return result


@router.get("/query")
def query_business(
    q: Annotated[str, Query(min_length=1, max_length=4000)],
    user_id: Annotated[str, Query(min_length=1, max_length=100)] = "api-user",
) -> dict[str, object]:
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="请输入需要测试的问题")
    reply = get_business_agent().answer(
        f"admin-test:{user_id}",
        q,
        can_view_cost=True,
    )
    return {
        "intent": reply.intent,
        "intent_confidence": reply.intent_confidence,
        "intent_reason": reply.intent_reason,
        "intent_source": reply.intent_source,
        "understood_query": reply.understood_query,
        "content": reply.content,
        "sources": reply.sources,
    }
