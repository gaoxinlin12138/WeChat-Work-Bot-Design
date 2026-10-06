from types import SimpleNamespace

import pytest

from app.business.intent import Intent, MediaAction
from app.business.service import RoutedReply
from app.wecom import long_connection as wecom


def frame(msgid="message-1", chatid="group-1", user="employee-1", text="0689价格"):
    return {
        "body": {
            "msgid": msgid,
            "chattype": "group" if chatid else "single",
            "chatid": chatid,
            "from": {"userid": user},
            "text": {"content": text},
        }
    }


class FakeClient:
    def __init__(self, options):
        self.handlers = {}
        self.replies = []
        self.fail_progress = False
        self.fail_all = False

    def on(self, event):
        def register(handler):
            self.handlers[event] = handler
            return handler

        return register

    async def reply_stream(self, frame, stream_id, content, finish):
        self.replies.append((stream_id, content, finish))
        if self.fail_all or (not finish and self.fail_progress):
            raise ConnectionError("fake connection failure")


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setattr(wecom, "WSClient", FakeClient)
    calls = {"answer": [], "claims": [], "permissions": []}

    def answer(key, content, **kwargs):
        calls["answer"].append((key, content))
        calls["permissions"].append(kwargs)
        return RoutedReply(Intent.PRODUCT, "产品查询成功", [])

    class FakeDirectory:
        async def can_view_product_cost(self, user_id):
            return user_id == "sales-user"

    def claim(message):
        calls["claims"].append(message)
        return calls["claims"].count(message) == 1

    store = SimpleNamespace(claim_message=claim)
    monkeypatch.setattr(wecom, "get_rag_agent", lambda: SimpleNamespace(store=store))
    monkeypatch.setattr(wecom, "get_business_agent", lambda: SimpleNamespace(answer=answer))
    monkeypatch.setattr(wecom, "get_wecom_directory", lambda: FakeDirectory())
    client = wecom.create_client(SimpleNamespace(wecom_bot_id="fake", wecom_bot_secret="fake"))
    return client, calls, store


@pytest.mark.asyncio
async def test_employee_context_is_isolated_between_groups_and_private_chat(receiver):
    client, calls, _ = receiver
    for number, chat in enumerate(("group-1", "group-2", None)):
        await client.handlers["message.text"](frame(msgid=str(number), chatid=chat))
    keys = [key for key, _ in calls["answer"]]
    assert keys == [
        "wecom:group:group-1:user:employee-1",
        "wecom:group:group-2:user:employee-1",
        "wecom:direct:employee-1",
    ]


@pytest.mark.asyncio
async def test_cost_permission_is_only_granted_to_sales_in_private_chat(receiver):
    client, calls, _ = receiver
    await client.handlers["message.text"](
        frame(msgid="private-sales", chatid=None, user="sales-user", text="成本价")
    )
    await client.handlers["message.text"](
        frame(msgid="group-sales", chatid="group-1", user="sales-user", text="成本价")
    )

    assert calls["permissions"][0]["can_view_cost"] is True
    assert calls["permissions"][1]["can_view_cost"] is False
    assert "不在群聊中展示" in calls["permissions"][1]["cost_access_message"]


def test_conversation_key_cannot_collide_by_colon_in_identifiers():
    assert wecom.conversation_key(frame(chatid="a:user:b", user="c")) != (
        wecom.conversation_key(frame(chatid="a", user="b:user:c"))
    )


@pytest.mark.asyncio
async def test_claim_failure_reports_error_without_running_business(receiver):
    client, calls, store = receiver

    def fail_claim(message):
        raise ConnectionError("claim unavailable")

    store.claim_message = fail_claim
    await client.handlers["message.text"](frame())
    assert calls["answer"] == []
    assert len(client.replies) == 1
    assert client.replies[0][2] is True
    assert "没有成功完成" in client.replies[0][1]


@pytest.mark.asyncio
async def test_duplicate_message_is_not_answered_or_sent_twice(receiver):
    client, calls, _ = receiver
    await client.handlers["message.text"](frame())
    await client.handlers["message.text"](frame())
    assert len(calls["answer"]) == 1
    assert len(client.replies) == 2
    assert len({item[0] for item in client.replies}) == 1
    assert sum(item[2] for item in client.replies) == 1


@pytest.mark.asyncio
async def test_live_stream_reuses_one_message_and_finishes_once(monkeypatch):
    client = FakeClient(None)
    captured_kwargs = {}

    def answer_streaming(key, content, publish, **kwargs):
        captured_kwargs.update(kwargs)
        publish("根据制度")
        publish("根据制度，员工应提前申请。")
        return RoutedReply(Intent.POLICY, "根据制度，员工应提前申请。\n\n引用依据：制度", [])

    monkeypatch.setattr(
        wecom,
        "get_business_agent",
        lambda: SimpleNamespace(answer_streaming=answer_streaming),
    )
    reply, streamed = await wecom.answer_with_live_stream(
        client, frame(), "employee", "怎么请假", "same-stream", request_id="message-123"
    )
    await wecom.reply_routed_result(client, frame(), reply, stream_id="same-stream")

    assert streamed is True
    assert {item[0] for item in client.replies} == {"same-stream"}
    assert client.replies[-1][2] is True
    assert sum(item[2] for item in client.replies) == 1
    assert captured_kwargs["thread_id"] == "employee"
    assert captured_kwargs["request_id"] == "message-123"


@pytest.mark.asyncio
async def test_expired_stream_pushes_final_answer_instead() -> None:
    class ExpiredStreamClient(FakeClient):
        def __init__(self):
            super().__init__(None)
            self.pushed = []

        async def reply_stream(self, frame, stream_id, content, finish):
            if finish:
                raise TimeoutError("reply frame expired")
            await super().reply_stream(frame, stream_id, content, finish)

        async def send_message(self, target, body):
            self.pushed.append((target, body))

    client = ExpiredStreamClient()
    await wecom.finish_stream_or_push(client, frame(chatid=None), "expired-stream", "最终答案")

    assert client.pushed == [
        ("employee-1", {"msgtype": "markdown", "markdown": {"content": "最终答案"}})
    ]


@pytest.mark.asyncio
async def test_progress_failure_still_delivers_final_answer_once(receiver):
    client, calls, _ = receiver
    client.fail_progress = True
    await client.handlers["message.text"](frame(text="公司的年假制度"))
    assert len(calls["answer"]) == 1
    assert len(client.replies) == 2
    assert client.replies[0][0] == client.replies[1][0]
    assert client.replies[-1][1:] == ("产品查询成功", True)


@pytest.mark.asyncio
async def test_missing_sender_never_uses_shared_unknown_session(receiver):
    client, calls, _ = receiver
    await client.handlers["message.text"](frame(user=None))
    assert calls["answer"] == []
    assert "没有成功完成" in client.replies[-1][1]


@pytest.mark.asyncio
async def test_failed_error_reply_does_not_escape_event_handler(receiver):
    client, calls, _ = receiver
    client.fail_all = True
    await client.handlers["message.text"](frame(user=None))
    assert calls["answer"] == []


@pytest.mark.asyncio
async def test_local_media_rejects_file_outside_upload_root(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    upload.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private content")
    monkeypatch.setattr(
        wecom,
        "get_settings",
        lambda: SimpleNamespace(
            business_media_upload_dir=str(upload),
        ),
    )
    with pytest.raises(ValueError, match="文件路径无效"):
        await wecom.load_media_bytes(str(outside))


@pytest.mark.asyncio
async def test_missing_source_type_cannot_bypass_media_root(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    upload.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-accessible")
    monkeypatch.setattr(
        wecom,
        "get_settings",
        lambda: SimpleNamespace(
            business_media_upload_dir=str(upload),
        ),
    )

    async def fail_upload(*args, **kwargs):
        raise AssertionError("禁止上传越界文件")

    monkeypatch.setattr(wecom, "cached_wecom_media_id", fail_upload)
    client = FakeClient(None)
    reply = RoutedReply(
        Intent.PRODUCT_MEDIA,
        "图片",
        [
            {"media_path": str(outside), "media_type": "image"},
        ],
        MediaAction.SEND,
    )
    await wecom.reply_routed_result(client, frame(), reply)
    assert client.replies[-1][1] == "图片发送失败，请稍后重试。"
