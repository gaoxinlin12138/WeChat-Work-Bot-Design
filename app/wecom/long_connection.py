import asyncio
import base64
import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Mapping
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
import structlog
from aibot import WSClient, WSClientOptions, generate_req_id
from PIL import Image

from app.agent import get_rag_agent
from app.business.intent import Intent, MediaAction
from app.business.media_policy import (
    WECOM_IMAGE_SAFE_MAX_BYTES,
    WECOM_IMAGE_SAFE_MAX_PIXELS,
    WECOM_VIDEO_OVERSIZE_MESSAGE,
    WECOM_VIDEO_REJECTED_MESSAGE,
    WECOM_VIDEO_SAFE_MAX_BYTES,
)
from app.business.service import RoutedReply, get_business_agent, get_business_store
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.messages import WELCOME_MESSAGE
from app.wecom.directory import get_wecom_directory
from app.wecom.health import WeComHealthReporter

logger = structlog.get_logger()

_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"
_MEDIA_CHUNK_BYTES = 512 * 1024
# 通用媒体只做读取保护；视频还会按更严格的安全线单独校验。
_MAX_MEDIA_BYTES = 10 * 1024 * 1024
_MAX_INLINE_IMAGE_BYTES = WECOM_IMAGE_SAFE_MAX_BYTES
_MAX_INLINE_IMAGE_PIXELS = WECOM_IMAGE_SAFE_MAX_PIXELS
_MEDIA_UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}
_MEDIA_UPLOAD_LOCK_LAST_USED: dict[str, float] = {}
_MEDIA_UPLOAD_LOCK_TTL = 600.0
_STREAM_UPDATE_INTERVAL = 0.28
_STREAM_MIN_GROWTH = 12
_PROGRESS_STAGES = (
    (2.0, "正在识别问题并选择资料来源…"),
    (5.0, "正在查询相关资料…"),
    (8.0, "正在整理回答…"),
)


class SafeSdkLogger:
    """屏蔽 SDK 原始消息帧，避免临时回复地址和员工消息进入日志。"""

    @staticmethod
    def _safe(message: object) -> str:
        text = str(message)
        text = re.sub(r"response_code=[^&\s\"']+", "response_code=[redacted]", text)
        return text[:1000]

    def debug(self, _message: object, *_args: object) -> None:
        # SDK 的 DEBUG 包含完整消息 body；生产和本地日志均不记录。
        return

    def info(self, message: object, *_args: object) -> None:
        logger.info("wecom_sdk_info", message=self._safe(message))

    def warn(self, message: object, *_args: object) -> None:
        logger.warning("wecom_sdk_warning", message=self._safe(message))

    def error(self, message: object, *_args: object) -> None:
        logger.error("wecom_sdk_error", message=self._safe(message))


def extract_text(frame: Mapping[str, Any]) -> str:
    """从企业微信长连接消息帧中安全提取文字内容。"""
    body = frame.get("body")
    if not isinstance(body, Mapping):
        return ""
    text = body.get("text")
    if not isinstance(text, Mapping):
        return ""
    content = text.get("content")
    return content.strip() if isinstance(content, str) else ""


def require_bot_credentials(settings: Settings) -> tuple[str, str]:
    missing = []
    if not settings.wecom_bot_id:
        missing.append("WECOM_BOT_ID")
    if not settings.wecom_bot_secret:
        missing.append("WECOM_BOT_SECRET")
    if missing:
        raise RuntimeError(f"企业微信长连接尚未配置：{', '.join(missing)}")
    return settings.wecom_bot_id, settings.wecom_bot_secret


async def load_media_bytes(
    storage_path: str,
    *,
    max_bytes: int | None = None,
    oversize_message: str = "媒体文件超过企业微信临时素材限制",
) -> tuple[bytes, str]:
    size_limit = _MAX_MEDIA_BYTES if max_bytes is None else max_bytes
    parsed = urlparse(storage_path)
    if parsed.scheme in {"http", "https"}:
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("外部媒体链接必须使用 HTTPS 且不能包含用户凭据")
        hostname = parsed.hostname
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        except (OSError, ValueError) as exc:
            raise ValueError("外部媒体域名无法解析") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("外部媒体链接指向受限制的网络地址")
        chunks: list[bytes] = []
        total = 0
        # Do not follow redirects: the destination must be validated before every request.
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as http_client:
            async with http_client.stream("GET", storage_path) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("外部媒体链接不允许重定向")
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > size_limit:
                    raise ValueError(oversize_message)
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > size_limit:
                        raise ValueError(oversize_message)
                    chunks.append(chunk)
        filename = Path(unquote(parsed.path)).name or "product-media"
        return b"".join(chunks), filename
    path = Path(storage_path).resolve()
    settings = get_settings()
    allowed_dirs = [Path(settings.business_media_upload_dir).resolve()]
    knowledge_dir = getattr(settings, "knowledge_upload_dir", None)
    if knowledge_dir:
        allowed_dirs.append(Path(knowledge_dir).resolve())
    if not any(path.is_relative_to(directory) for directory in allowed_dirs):
        raise ValueError("产品媒体文件路径无效")
    if not path.is_file():
        raise FileNotFoundError("产品媒体文件不存在")
    if path.stat().st_size > size_limit:
        raise ValueError(oversize_message)
    return await asyncio.to_thread(path.read_bytes), path.name


async def upload_wecom_media(
    client: WSClient, media_type: str, content: bytes, filename: str
) -> str:
    if media_type == "video" and len(content) > WECOM_VIDEO_SAFE_MAX_BYTES:
        raise ValueError(WECOM_VIDEO_OVERSIZE_MESSAGE)
    total_chunks = (len(content) + _MEDIA_CHUNK_BYTES - 1) // _MEDIA_CHUNK_BYTES
    if not content or total_chunks > 100:
        raise ValueError("媒体文件为空或超过企业微信临时素材限制")
    digest = hashlib.md5(content, usedforsecurity=False).hexdigest()
    manager = client._ws_manager  # SDK 1.0.2 尚未封装官方临时素材上传方法。
    init = await manager.send_reply(
        generate_req_id(_UPLOAD_MEDIA_INIT),
        {
            "type": media_type,
            "filename": filename,
            "total_size": len(content),
            "total_chunks": total_chunks,
            "md5": digest,
        },
        _UPLOAD_MEDIA_INIT,
    )
    upload_id = (init.get("body") or {}).get("upload_id")
    if not upload_id:
        raise RuntimeError("企业微信没有返回临时素材上传编号")
    for chunk_index in range(total_chunks):
        start = chunk_index * _MEDIA_CHUNK_BYTES
        chunk = content[start : start + _MEDIA_CHUNK_BYTES]
        for attempt in range(3):
            try:
                await manager.send_reply(
                    generate_req_id(_UPLOAD_MEDIA_CHUNK),
                    {
                        "upload_id": upload_id,
                        "chunk_index": chunk_index,
                        "base64_data": base64.b64encode(chunk).decode("ascii"),
                    },
                    _UPLOAD_MEDIA_CHUNK,
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
    finished = await manager.send_reply(
        generate_req_id(_UPLOAD_MEDIA_FINISH),
        {"upload_id": upload_id},
        _UPLOAD_MEDIA_FINISH,
    )
    media_id = (finished.get("body") or {}).get("media_id")
    if not media_id:
        raise RuntimeError("企业微信没有返回临时素材 media_id")
    return str(media_id)


def _video_push_failure_message(exc: Exception) -> str:
    detail = str(exc).lower()
    if "40011" in detail or "invalid video size" in detail:
        return WECOM_VIDEO_REJECTED_MESSAGE
    return (
        "企业微信暂时无法接收这个视频，请确认文件为9MB以内的MP4"
        "（H.264/AAC，建议720×1280、30fps）后重试"
    )


async def cached_wecom_media_id(
    client: WSClient,
    media_type: str,
    content: bytes,
    filename: str,
    *,
    force_refresh: bool = False,
) -> tuple[str, str]:
    """复用有效 media_id；过期、缺失或强制刷新时自动重新上传。"""
    content_hash = hashlib.sha256(content).hexdigest()
    lock_key = f"{media_type}:{content_hash}"
    lock = _MEDIA_UPLOAD_LOCKS.setdefault(lock_key, asyncio.Lock())
    _MEDIA_UPLOAD_LOCK_LAST_USED[lock_key] = time.monotonic()
    try:
        async with lock:
            store = None
            try:
                store = get_business_store()
                if not force_refresh:
                    cached = await asyncio.to_thread(
                        store.get_wecom_media_id, content_hash, media_type
                    )
                    if cached:
                        return cached, content_hash
            except Exception as exc:
                await logger.awarning(
                    "wecom_media_cache_read_failed", error_type=type(exc).__name__
                )
                store = None

            media_id = ""
            for attempt in range(3):
                try:
                    media_id = await upload_wecom_media(client, media_type, content, filename)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            if store is not None:
                try:
                    await asyncio.to_thread(
                        store.save_wecom_media_id,
                        content_hash,
                        media_type,
                        media_id,
                    )
                except Exception as exc:
                    await logger.awarning(
                        "wecom_media_cache_write_failed", error_type=type(exc).__name__
                    )
            return media_id, content_hash
    finally:
        _MEDIA_UPLOAD_LOCK_LAST_USED[lock_key] = time.monotonic()
        # Remove idle per-content locks so a stream of unique media cannot leak memory.
        waiters = getattr(lock, "_waiters", None)
        if not lock.locked() and not waiters:
            _MEDIA_UPLOAD_LOCKS.pop(lock_key, None)
            _MEDIA_UPLOAD_LOCK_LAST_USED.pop(lock_key, None)
        elif len(_MEDIA_UPLOAD_LOCKS) > 1024:
            cutoff = time.monotonic() - _MEDIA_UPLOAD_LOCK_TTL
            for key, used_at in list(_MEDIA_UPLOAD_LOCK_LAST_USED.items()):
                candidate = _MEDIA_UPLOAD_LOCKS.get(key)
                if used_at < cutoff and candidate is not None and not candidate.locked():
                    _MEDIA_UPLOAD_LOCKS.pop(key, None)
                    _MEDIA_UPLOAD_LOCK_LAST_USED.pop(key, None)


async def invalidate_wecom_media_id(content_hash: str, media_type: str) -> None:
    try:
        await asyncio.to_thread(
            get_business_store().invalidate_wecom_media_id,
            content_hash,
            media_type,
        )
    except Exception as exc:
        await logger.awarning("wecom_media_cache_invalidate_failed", error_type=type(exc).__name__)


def _media_message_body(
    media_type: str,
    media_id: str,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    media: dict[str, object] = {"media_id": media_id}
    if media_type == "video":
        if title:
            media["title"] = title[:42]
        if description:
            media["description"] = description
    return {"msgtype": media_type, media_type: media}


async def reply_cached_media(
    client: WSClient,
    frame: dict[str, Any],
    media_type: str,
    content: bytes,
    filename: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> None:
    for attempt in range(2):
        media_id, content_hash = await cached_wecom_media_id(
            client,
            media_type,
            content,
            filename,
            force_refresh=attempt > 0,
        )
        try:
            await client.reply(
                frame,
                _media_message_body(media_type, media_id, title, description),
            )
            return
        except Exception:
            await invalidate_wecom_media_id(content_hash, media_type)
            if attempt == 1:
                raise


async def send_cached_media(
    client: WSClient,
    target: str,
    media_type: str,
    content: bytes,
    filename: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> None:
    for attempt in range(2):
        media_id, content_hash = await cached_wecom_media_id(
            client,
            media_type,
            content,
            filename,
            force_refresh=attempt > 0,
        )
        try:
            await client.send_message(
                target,
                _media_message_body(media_type, media_id, title, description),
            )
            return
        except Exception:
            await invalidate_wecom_media_id(content_hash, media_type)
            if attempt == 1:
                raise


def normalize_inline_image(content: bytes) -> bytes:
    """转为企业微信客户端兼容性更稳定的无透明通道 RGB JPEG。"""
    with Image.open(BytesIO(content)) as source:
        source.load()
        if source.width * source.height > _MAX_INLINE_IMAGE_PIXELS:
            raise ValueError("图片像素尺寸过大")
        if source.mode in {"RGBA", "LA"} or "transparency" in source.info:
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = source.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        normalized = output.getvalue()
    if len(normalized) > _MAX_INLINE_IMAGE_BYTES:
        raise ValueError("转换后的图片超过 10MB")
    return normalized


def extract_chat_target(frame: Mapping[str, Any]) -> str:
    """提取主动推送的目标；群聊使用 chatid，单聊使用发送人 userid。"""
    body = frame.get("body")
    if not isinstance(body, Mapping):
        return ""
    sender = body.get("from")
    user_id = sender.get("userid") if isinstance(sender, Mapping) else None
    chat_id = body.get("chatid")
    target = chat_id if body.get("chattype") == "group" else user_id or chat_id
    return str(target) if target else ""


def extract_sender_user_id(frame: Mapping[str, Any]) -> str:
    body = frame.get("body")
    if not isinstance(body, Mapping):
        return ""
    sender = body.get("from")
    user_id = sender.get("userid") if isinstance(sender, Mapping) else None
    return str(user_id).strip() if user_id else ""


def conversation_key(frame: Mapping[str, Any]) -> str:
    """同一员工在不同群及私聊的产品选择、追问和消息历史彼此隔离。"""
    body = frame.get("body")
    if not isinstance(body, Mapping):
        raise ValueError("消息缺少会话信息")
    user_id = extract_sender_user_id(frame)
    if not user_id:
        raise ValueError("消息缺少发送人信息")
    user = quote(user_id, safe="")
    if body.get("chattype") == "group":
        chat_id = body.get("chatid")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError("群消息缺少群会话信息")
        return f"wecom:group:{quote(chat_id, safe='')}:user:{user}"
    return f"wecom:direct:{user}"


async def answer_with_live_stream(
    client: WSClient,
    frame: dict[str, Any],
    user_id: str,
    content: str,
    stream_id: str,
    *,
    can_view_cost: bool = False,
    cost_access_message: str = "你当前没有查看成本价和采购价的权限。",
    request_id: str | None = None,
) -> tuple[RoutedReply, bool]:
    """在线程中运行同步业务链路，并节流转发模型的累计流式正文。"""
    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[str] = asyncio.Queue()

    def publish(partial: str) -> None:
        loop.call_soon_threadsafe(updates.put_nowait, partial)

    agent = get_business_agent()
    streaming_answer = getattr(agent, "answer_streaming", None)
    if callable(streaming_answer):
        worker = asyncio.create_task(
            asyncio.to_thread(
                streaming_answer,
                user_id,
                content,
                publish,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
                thread_id=user_id,
                request_id=request_id,
            )
        )
    else:
        worker = asyncio.create_task(
            asyncio.to_thread(
                agent.answer,
                user_id,
                content,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
                thread_id=user_id,
                request_id=request_id,
            )
        )

    last_sent = ""
    last_sent_at = 0.0
    update_failed = False
    started_at = time.monotonic()
    progress_index = 0
    while not worker.done() or not updates.empty():
        try:
            partial = await asyncio.wait_for(updates.get(), timeout=0.12)
        except TimeoutError:
            elapsed = time.monotonic() - started_at
            if (
                not update_failed
                and progress_index < len(_PROGRESS_STAGES)
                and elapsed >= _PROGRESS_STAGES[progress_index][0]
            ):
                try:
                    await client.reply_stream(
                        frame,
                        stream_id,
                        _PROGRESS_STAGES[progress_index][1],
                        False,
                    )
                except Exception as exc:
                    update_failed = True
                    await logger.awarning(
                        "wecom_progress_stage_failed", error_type=type(exc).__name__
                    )
                progress_index += 1
            continue
        while not updates.empty():
            partial = updates.get_nowait()
        if update_failed or not partial or partial == last_sent:
            continue
        if len(partial) - len(last_sent) < _STREAM_MIN_GROWTH and not worker.done():
            continue
        delay = _STREAM_UPDATE_INTERVAL - (time.monotonic() - last_sent_at)
        if delay > 0:
            await asyncio.sleep(delay)
            while not updates.empty():
                partial = updates.get_nowait()
        try:
            await client.reply_stream(frame, stream_id, partial, False)
            last_sent = partial
            last_sent_at = time.monotonic()
        except Exception as exc:
            update_failed = True
            await logger.awarning("wecom_stream_update_failed", error_type=type(exc).__name__)
    return await worker, bool(last_sent)


async def finish_stream_or_push(
    client: WSClient,
    frame: dict[str, Any],
    stream_id: str,
    content: str,
) -> None:
    """结束流式消息；原回复帧失效时改用主动消息保证答案可达。"""
    try:
        await client.reply_stream(frame, stream_id, content, True)
        return
    except Exception as exc:
        await logger.awarning("wecom_final_stream_failed", error_type=type(exc).__name__)
    target = extract_chat_target(frame)
    if not target:
        raise RuntimeError("无法取得企业微信主动消息目标")
    await client.send_message(
        target,
        {"msgtype": "markdown", "markdown": {"content": content}},
    )
    await logger.ainfo("wecom_final_answer_pushed_after_stream_failure")


async def animate_ready_text(
    client: WSClient, frame: dict[str, Any], stream_id: str, content: str
) -> None:
    """固定模板没有模型 token 流，按较大的文本块平滑展示，避免逐字刷接口。"""
    if len(content) < 56:
        return
    cuts = range(48, min(len(content), 240), 48)
    for end in cuts:
        await client.reply_stream(frame, stream_id, content[:end], False)
        await asyncio.sleep(0.09)


async def reply_routed_result(
    client: WSClient,
    frame: dict[str, Any],
    reply: RoutedReply,
    stream_id: str | None = None,
) -> None:
    file_sources = [source for source in reply.sources if source.get("media_type") == "file"]
    if file_sources and reply.media_action == MediaAction.SEND:
        source = file_sources[0]
        try:
            content, filename = await load_media_bytes(
                str(source["media_path"]),
                oversize_message="检测报告 PDF 超过企业微信临时文件限制",
            )
            target = extract_chat_target(frame)
            if stream_id and target:
                await send_cached_media(client, target, "file", content, filename)
            else:
                await reply_cached_media(client, frame, "file", content, filename)
            if stream_id:
                await finish_stream_or_push(client, frame, stream_id, reply.content)
        except (FileNotFoundError, ValueError) as exc:
            message = f"检测报告发送失败：{exc or '原文件不可用'}。"
            await finish_stream_or_push(
                client, frame, stream_id or generate_req_id("stream"), message
            )
        except Exception as exc:
            await logger.aerror("wecom_document_file_push_failed", error_type=type(exc).__name__)
            await finish_stream_or_push(
                client,
                frame,
                stream_id or generate_req_id("stream"),
                "检测报告发送失败，请稍后重试。",
            )
        return
    if reply.intent == Intent.PRODUCT:
        await finish_stream_or_push(
            client, frame, stream_id or generate_req_id("stream"), reply.content
        )
        return
    if (
        reply.intent != Intent.PRODUCT_MEDIA
        or reply.media_action != MediaAction.SEND
        or not reply.sources
    ):
        await finish_stream_or_push(
            client, frame, stream_id or generate_req_id("stream"), reply.content
        )
        return
    image_sources = [source for source in reply.sources if source.get("media_type") == "image"]
    if image_sources:
        target = extract_chat_target(frame)
        replied = False
        failed = 0
        for source in image_sources[:10]:
            try:
                content, _ = await load_media_bytes(str(source["media_path"]))
                normalized = await asyncio.to_thread(normalize_inline_image, content)
                if stream_id and target:
                    await send_cached_media(
                        client,
                        target,
                        "image",
                        normalized,
                        "product-image.jpg",
                    )
                    replied = True
                elif not replied:
                    await reply_cached_media(
                        client,
                        frame,
                        "image",
                        normalized,
                        "product-image.jpg",
                    )
                    replied = True
                elif target:
                    await send_cached_media(
                        client,
                        target,
                        "image",
                        normalized,
                        "product-image.jpg",
                    )
            except Exception as exc:
                failed += 1
                await logger.aerror(
                    "wecom_product_image_push_failed",
                    error_type=type(exc).__name__,
                )
        if not replied:
            final_content = "图片发送失败，请稍后重试。"
        elif failed:
            final_content = f"{reply.content}\n部分图片发送失败（{failed} 张），其余图片已发送。"
        else:
            final_content = reply.content
        if stream_id:
            await finish_stream_or_push(client, frame, stream_id, final_content)
        elif failed and target:
            notice_sent = False
            try:
                await client.send_message(
                    target,
                    {"msgtype": "markdown", "markdown": {"content": final_content}},
                )
                notice_sent = True
            except Exception as exc:
                await logger.awarning(
                    "wecom_product_image_result_notice_failed", error_type=type(exc).__name__
                )
            if not notice_sent:
                await client.reply_stream(frame, generate_req_id("stream"), final_content, True)
        elif failed:
            await client.reply_stream(frame, generate_req_id("stream"), final_content, True)
        return
    video_source = next(
        (source for source in reply.sources if source.get("media_type") == "video"),
        None,
    )
    if video_source:
        target = extract_chat_target(frame)
        try:
            content, filename = await load_media_bytes(
                str(video_source["media_path"]),
                max_bytes=WECOM_VIDEO_SAFE_MAX_BYTES,
                oversize_message=WECOM_VIDEO_OVERSIZE_MESSAGE,
            )
            if stream_id and target:
                await send_cached_media(
                    client,
                    target,
                    "video",
                    content,
                    filename,
                    title=str(video_source.get("title") or "产品安装视频"),
                    description="产品库中的安装使用视频",
                )
            else:
                await reply_cached_media(
                    client,
                    frame,
                    "video",
                    content,
                    filename,
                    title=str(video_source.get("title") or "产品安装视频"),
                    description="产品库中的安装使用视频",
                )
            if stream_id:
                await finish_stream_or_push(client, frame, stream_id, reply.content)
        except (FileNotFoundError, ValueError) as exc:
            # 不再让失败停留在“正在发送”；把平台限制或文件问题明确告诉员工。
            message = str(exc) or "视频文件无法发送"
            if stream_id:
                await finish_stream_or_push(client, frame, stream_id, f"视频发送失败：{message}。")
            else:
                await client.reply_stream(
                    frame,
                    generate_req_id("stream"),
                    f"视频发送失败：{message}。",
                    True,
                )
        except Exception as exc:
            await logger.aerror("wecom_product_video_push_failed", error_type=type(exc).__name__)
            message = _video_push_failure_message(exc)
            if stream_id:
                await finish_stream_or_push(client, frame, stream_id, f"视频发送失败：{message}。")
            else:
                await client.reply_stream(
                    frame,
                    generate_req_id("stream"),
                    f"视频发送失败：{message}。",
                    True,
                )
        return
    await finish_stream_or_push(
        client, frame, stream_id or generate_req_id("stream"), reply.content
    )


def create_client(
    settings: Settings | None = None,
    health_reporter: WeComHealthReporter | None = None,
) -> WSClient:
    current = settings or get_settings()
    bot_id, bot_secret = require_bot_credentials(current)
    client = WSClient(
        WSClientOptions(
            bot_id=bot_id,
            secret=bot_secret,
            max_reconnect_attempts=-1,
            heartbeat_interval=30_000,
            logger=SafeSdkLogger(),
        )
    )

    @client.on("connected")
    async def on_connected() -> None:
        if health_reporter:
            await health_reporter.publish("connecting")
        await logger.ainfo("wecom_long_connection_opened")

    @client.on("authenticated")
    async def on_authenticated() -> None:
        if health_reporter:
            await health_reporter.publish("online")
        await logger.ainfo("wecom_long_connection_authenticated")

    @client.on("reconnecting")
    async def on_reconnecting(attempt: int) -> None:
        if health_reporter:
            await health_reporter.publish("reconnecting", reason=f"attempt:{attempt}")
        await logger.awarning("wecom_long_connection_reconnecting", attempt=attempt)

    @client.on("disconnected")
    async def on_disconnected(reason: str) -> None:
        if health_reporter:
            await health_reporter.publish("offline", reason=reason)
        await logger.awarning("wecom_long_connection_disconnected", reason=reason)

    @client.on("error")
    async def on_error(error: Exception) -> None:
        if health_reporter:
            await health_reporter.publish("offline", reason=type(error).__name__)
        await logger.aerror("wecom_long_connection_error", error_type=type(error).__name__)

    @client.on("event.enter_chat")
    async def on_enter_chat(frame: dict[str, Any]) -> None:
        await client.reply_welcome(
            frame,
            {
                "msgtype": "text",
                "text": {"content": WELCOME_MESSAGE},
            },
        )

    @client.on("message.text")
    async def on_text(frame: dict[str, Any]) -> None:
        started_at = time.monotonic()
        content = extract_text(frame)
        if not content:
            return
        body = frame.get("body", {})
        message_id = body.get("msgid")
        progress_stream_id = None
        try:
            if message_id:
                claimed = await asyncio.to_thread(
                    get_rag_agent().store.claim_message, str(message_id)
                )
                if not claimed:
                    await logger.awarning("wecom_duplicate_message_ignored", message_id=message_id)
                    return
            await logger.ainfo(
                "wecom_long_connection_text_received",
                message_id=message_id,
                chat_type=body.get("chattype"),
            )
            user_id = conversation_key(frame)
            sender_user_id = extract_sender_user_id(frame)
            can_view_cost = False
            cost_access_message = "你当前没有查看成本价和采购价的权限。"
            if body.get("chattype") == "group":
                cost_access_message = (
                    "成本价和采购价不在群聊中展示；营销中心成员请私聊机器人查询。"
                )
            else:
                try:
                    can_view_cost = await get_wecom_directory().can_view_product_cost(
                        sender_user_id
                    )
                except Exception as exc:
                    cost_access_message = (
                        "暂时无法确认你的部门权限；为保护敏感价格，当前不展示成本价和采购价。"
                    )
                    await logger.aerror(
                        "wecom_directory_access_failed",
                        error_type=type(exc).__name__,
                    )
            progress_stream_id = generate_req_id("stream")
            try:
                await client.reply_stream(
                    frame,
                    progress_stream_id,
                    "正在理解你的问题…",
                    False,
                )
            except Exception as exc:
                # 进度提示失败不丢弃已成功领取的查询；最终结果继续使用同一 stream。
                await logger.awarning(
                    "wecom_progress_reply_failed",
                    error_type=type(exc).__name__,
                )
            agent_reply, streamed = await answer_with_live_stream(
                client,
                frame,
                user_id,
                content,
                progress_stream_id,
                can_view_cost=can_view_cost,
                cost_access_message=cost_access_message,
                request_id=str(message_id) if message_id else None,
            )
            if (
                not streamed
                and agent_reply.intent != Intent.PRODUCT_MEDIA
                and len(agent_reply.content) >= 56
            ):
                try:
                    await animate_ready_text(client, frame, progress_stream_id, agent_reply.content)
                except Exception as exc:
                    await logger.awarning(
                        "wecom_static_stream_failed", error_type=type(exc).__name__
                    )
            await reply_routed_result(client, frame, agent_reply, stream_id=progress_stream_id)
            await logger.ainfo(
                "wecom_answer_completed",
                message_id=message_id,
                intent=str(agent_reply.intent),
                intent_source=agent_reply.intent_source,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
        except Exception as exc:
            await logger.aerror(
                "wecom_answer_failed",
                message_id=message_id,
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            # 领取幂等记录失败也走此处，绝不绕过领取继续查询或重复发送业务结果。
            try:
                await client.reply_stream(
                    frame,
                    progress_stream_id or generate_req_id("stream"),
                    "这次查询没有成功完成，请稍后再试一次。",
                    True,
                )
            except Exception as reply_exc:
                await logger.aerror(
                    "wecom_failure_reply_failed",
                    error_type=type(reply_exc).__name__,
                )

    return client


async def run_bot() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    health_reporter = WeComHealthReporter(settings)
    await health_reporter.publish("starting")
    client = create_client(settings, health_reporter)
    heartbeat_task = asyncio.create_task(health_reporter.heartbeat_loop())
    try:
        await client.connect()
        await asyncio.Event().wait()
    finally:
        client.disconnect()
        await health_reporter.publish("offline", reason="stopped")
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
