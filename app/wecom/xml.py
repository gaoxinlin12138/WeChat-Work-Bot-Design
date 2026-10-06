from dataclasses import dataclass
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from defusedxml import ElementTree


class WeComXmlError(ValueError):
    """企业微信XML格式无效。"""


@dataclass(frozen=True)
class IncomingMessage:
    to_user: str
    from_user: str
    create_time: int
    message_type: str
    content: str | None
    message_id: str | None


def _required_text(root: Element, name: str) -> str:
    value = root.findtext(name)
    if value is None or not value.strip():
        raise WeComXmlError(f"XML缺少{name}")
    return value.strip()


def parse_encrypted_xml(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WeComXmlError("外层XML格式无效") from exc
    return _required_text(root, "Encrypt")


def parse_message_xml(payload: str) -> IncomingMessage:
    try:
        root = ElementTree.fromstring(payload.encode())
        create_time = int(_required_text(root, "CreateTime"))
    except (ElementTree.ParseError, ValueError) as exc:
        raise WeComXmlError("消息XML格式无效") from exc
    return IncomingMessage(
        to_user=_required_text(root, "ToUserName"),
        from_user=_required_text(root, "FromUserName"),
        create_time=create_time,
        message_type=_required_text(root, "MsgType"),
        content=root.findtext("Content"),
        message_id=root.findtext("MsgId"),
    )


def build_text_reply(message: IncomingMessage, content: str, timestamp: int) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{message.from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{message.to_user}]]></FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content>{escape(content)}</Content>"
        "</xml>"
    )


def build_encrypted_reply(
    encrypted: str,
    signature: str,
    timestamp: str,
    nonce: str,
) -> str:
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )
