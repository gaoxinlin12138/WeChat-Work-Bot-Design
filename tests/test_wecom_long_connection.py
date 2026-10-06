from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.business.intent import Intent, MediaAction
from app.business.service import RoutedReply
from app.config import Settings
from app.wecom.long_connection import (
    _video_push_failure_message,
    cached_wecom_media_id,
    extract_text,
    reply_routed_result,
    require_bot_credentials,
    upload_wecom_media,
)


def test_extract_text() -> None:
    frame = {"body": {"text": {"content": " 你好 "}}}
    assert extract_text(frame) == "你好"


def test_extract_text_handles_invalid_frame() -> None:
    assert extract_text({}) == ""
    assert extract_text({"body": {"text": None}}) == ""


def test_require_bot_credentials() -> None:
    settings = Settings(
        _env_file=None,
        wecom_bot_id="bot-id",
        wecom_bot_secret="bot-secret",
    )
    assert require_bot_credentials(settings) == ("bot-id", "bot-secret")


def test_require_bot_credentials_reports_missing_names() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(RuntimeError, match="WECOM_BOT_ID, WECOM_BOT_SECRET"):
        require_bot_credentials(settings)


@pytest.mark.asyncio
async def test_wecom_media_id_reuses_valid_database_cache(monkeypatch) -> None:
    class FakeStore:
        def get_wecom_media_id(self, content_hash, media_type):
            self.read = (content_hash, media_type)
            return "cached-media-id"

    store = FakeStore()
    monkeypatch.setattr("app.wecom.long_connection.get_business_store", lambda: store)

    async def fail_upload(*args, **kwargs):
        raise AssertionError("有效缓存不应重新上传")

    monkeypatch.setattr("app.wecom.long_connection.upload_wecom_media", fail_upload)

    media_id, content_hash = await cached_wecom_media_id(
        object(), "image", b"same-image", "product.jpg"
    )

    assert media_id == "cached-media-id"
    assert store.read == (content_hash, "image")


@pytest.mark.asyncio
async def test_media_reply_uses_official_image_media_message(tmp_path, monkeypatch) -> None:
    buffer = BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(buffer, format="PNG")
    content = buffer.getvalue()
    image = tmp_path / "main.png"
    image.write_bytes(content)

    uploads = []

    async def fake_cached(client, media_type, uploaded_content, filename, *, force_refresh=False):
        uploads.append((media_type, uploaded_content, filename))
        return "image-media-id", "content-hash"

    monkeypatch.setattr("app.wecom.long_connection.cached_wecom_media_id", fake_cached)
    monkeypatch.setattr(
        "app.wecom.long_connection.get_settings",
        lambda: SimpleNamespace(business_media_upload_dir=str(tmp_path)),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.replies = []

        async def reply(self, frame, body):
            self.replies.append((frame, body))

    client = FakeClient()
    reply = RoutedReply(
        Intent.PRODUCT_MEDIA,
        "找到产品图",
        [{"media_type": "image", "media_path": str(image), "title": "产品图"}],
        MediaAction.SEND,
    )
    await reply_routed_result(client, {"headers": {"req_id": "1"}}, reply)

    assert len(client.replies) == 1
    assert client.replies[0][1] == {
        "msgtype": "image",
        "image": {"media_id": "image-media-id"},
    }
    assert uploads[0][0] == "image"
    assert uploads[0][1].startswith(b"\xff\xd8")
    assert uploads[0][2] == "product-image.jpg"


@pytest.mark.asyncio
async def test_product_overview_is_one_text_message_without_card() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.replies = []
            self.sent = []

        async def reply_stream(self, *args, **kwargs):
            self.replies.append((args, kwargs))

    client = FakeClient()
    reply = RoutedReply(
        Intent.PRODUCT,
        "产品型号：LTA-822\n材质：全铝\n图片：共2张\n视频：共1个",
        [{"source_type": "product", "source_id": "product-1"}],
    )

    await reply_routed_result(client, {"headers": {"req_id": "1"}}, reply)

    assert len(client.replies) == 1
    assert client.replies[0][0][2] == reply.content
    assert client.sent == []


@pytest.mark.asyncio
async def test_plain_product_query_remains_one_text_message() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.replies = []
            self.sent = []

        async def reply_stream(self, *args, **kwargs):
            self.replies.append((args, kwargs))

        async def send_message(self, target, body):
            self.sent.append((target, body))

    client = FakeClient()
    reply = RoutedReply(
        Intent.PRODUCT,
        "0689销售价为299元。",
        [{"source_type": "product", "source_id": "product-1"}],
    )

    await reply_routed_result(client, {"headers": {"req_id": "1"}}, reply)

    assert len(client.replies) == 1
    assert client.replies[0][1] == {}
    assert client.sent == []


@pytest.mark.asyncio
async def test_upload_wecom_video_uses_official_three_step_protocol() -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.commands = []

        async def send_reply(self, req_id, body, command):
            self.commands.append((req_id, body, command))
            if command == "aibot_upload_media_init":
                return {"body": {"upload_id": "upload-1"}}
            if command == "aibot_upload_media_finish":
                return {"body": {"media_id": "media-1"}}
            return {"body": {}}

    class FakeClient:
        _ws_manager = FakeManager()

    client = FakeClient()
    media_id = await upload_wecom_media(client, "video", b"video", "demo.mp4")

    assert media_id == "media-1"
    assert [item[2] for item in client._ws_manager.commands] == [
        "aibot_upload_media_init",
        "aibot_upload_media_chunk",
        "aibot_upload_media_finish",
    ]


@pytest.mark.asyncio
async def test_oversized_video_finishes_stream_with_clear_failure(
    tmp_path, monkeypatch
) -> None:
    video = tmp_path / "large.mp4"
    video.write_bytes(b"123456")
    monkeypatch.setattr("app.wecom.long_connection.WECOM_VIDEO_SAFE_MAX_BYTES", 5)
    monkeypatch.setattr(
        "app.wecom.long_connection.get_settings",
        lambda: SimpleNamespace(business_media_upload_dir=str(tmp_path)),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.replies = []

        async def reply_stream(self, *args, **kwargs):
            self.replies.append((args, kwargs))

    client = FakeClient()
    reply = RoutedReply(
        Intent.PRODUCT_MEDIA,
        "产品视频正在发送",
        [{"media_type": "video", "media_path": str(video), "title": "产品视频"}],
        MediaAction.SEND,
    )
    frame = {
        "headers": {"req_id": "1"},
        "body": {"from": {"userid": "user-1"}, "chattype": "single"},
    }

    await reply_routed_result(client, frame, reply, stream_id="stream-1")

    assert len(client.replies) == 1
    assert client.replies[0][0][2] == (
        "视频发送失败：视频超过企业微信安全发送上限（9MB），"
        "请压缩后重新上传。"
    )
    assert client.replies[0][0][3] is True


@pytest.mark.asyncio
async def test_video_upload_rejects_content_above_safe_limit(monkeypatch) -> None:
    monkeypatch.setattr("app.wecom.long_connection.WECOM_VIDEO_SAFE_MAX_BYTES", 5)

    with pytest.raises(ValueError, match="9MB"):
        await upload_wecom_media(object(), "video", b"123456", "demo.mp4")


def test_wecom_40011_has_actionable_video_guidance() -> None:
    message = _video_push_failure_message(RuntimeError("40011 invalid video size"))

    assert "9MB以内" in message
    assert "H.264/AAC" in message
