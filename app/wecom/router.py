import secrets
import time

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.config import get_settings
from app.wecom.crypto import WeComCrypto, WeComCryptoError, calculate_signature
from app.wecom.xml import (
    WeComXmlError,
    build_encrypted_reply,
    build_text_reply,
    parse_encrypted_xml,
    parse_message_xml,
)

router = APIRouter(prefix="/wxwork", tags=["wecom"])
logger = structlog.get_logger()


def _crypto() -> WeComCrypto:
    settings = get_settings()
    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="旧版回调接口已停用")
    required = (
        ("WECOM_CORP_ID", settings.wecom_corp_id),
        ("WECOM_CALLBACK_TOKEN", settings.wecom_callback_token),
        ("WECOM_ENCODING_AES_KEY", settings.wecom_encoding_aes_key),
    )
    missing = [name for name, value in required if not value]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"企业微信回调尚未配置：{', '.join(missing)}",
        )
    return WeComCrypto(
        token=settings.wecom_callback_token or "",
        encoding_aes_key=settings.wecom_encoding_aes_key or "",
        receive_id=settings.wecom_corp_id or "",
    )


@router.get("/callback", response_class=Response)
async def verify_callback(
    msg_signature: str = Query(),
    timestamp: str = Query(),
    nonce: str = Query(),
    echostr: str = Query(),
) -> Response:
    crypto = _crypto()
    try:
        crypto.verify_signature(msg_signature, timestamp, nonce, echostr)
        echo = crypto.decrypt(echostr)
    except WeComCryptoError as exc:
        await logger.awarning("wecom_callback_verification_failed", reason=str(exc))
        raise HTTPException(status_code=403, detail="回调验证失败") from exc
    return Response(content=echo, media_type="text/plain")


@router.post("/callback", response_class=Response)
async def receive_callback(
    request: Request,
    msg_signature: str = Query(),
    timestamp: str = Query(),
    nonce: str = Query(),
) -> Response:
    crypto = _crypto()
    try:
        encrypted = parse_encrypted_xml(await request.body())
        crypto.verify_signature(msg_signature, timestamp, nonce, encrypted)
        message = parse_message_xml(crypto.decrypt(encrypted))
    except (WeComCryptoError, WeComXmlError) as exc:
        await logger.awarning("wecom_message_rejected", reason=str(exc))
        raise HTTPException(status_code=403, detail="消息验证失败") from exc

    await logger.ainfo(
        "wecom_message_received",
        message_type=message.message_type,
        message_id=message.message_id,
        from_user=message.from_user,
    )
    if message.message_type != "text" or not message.content:
        return Response(content="success", media_type="text/plain")

    reply_xml = build_text_reply(
        message,
        f"联调成功，收到：{message.content}",
        timestamp=int(time.time()),
    )
    reply_encrypted = crypto.encrypt(reply_xml)
    reply_timestamp = str(int(time.time()))
    reply_nonce = secrets.token_hex(8)
    reply_signature = calculate_signature(
        crypto.token,
        reply_timestamp,
        reply_nonce,
        reply_encrypted,
    )
    response_xml = build_encrypted_reply(
        reply_encrypted,
        reply_signature,
        reply_timestamp,
        reply_nonce,
    )
    return Response(content=response_xml, media_type="application/xml")
