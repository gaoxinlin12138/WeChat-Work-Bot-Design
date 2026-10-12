from types import SimpleNamespace

from app.business.intent import Intent, IntentDecision, MediaAction
from app.business.graph.builder import _pending_media_is_model_only
from app.business.service import BusinessAgent
from app.business.store import BusinessResult


class _MediaStore:
    def search_products(self, query, *, limit, query_embedding):
        if "LY0653NBT-T" not in query:
            return []
        return [self.get_active_product("p4")]

    def query_products_v2(self, *_args, **_kwargs):
        raise AssertionError("media follow-up should not enter the product executor")

    def get_active_product(self, product_id: str):
        if product_id != "p4":
            return None
        return BusinessResult("product", "p4", "LY0653NBT-T", "产品型号：LY0653NBT-T", 1.0)

    def search_media(
        self,
        _query,
        _media_type,
        *,
        limit,
        related_product_id,
        media_role,
        include_all,
    ):
        assert related_product_id == "p4"
        return [
            BusinessResult(
                "media",
                "m4",
                "LY0653NBT-T 视频",
                "",
                1.0,
                "video",
                "data/product-video.mp4",
                "product_video",
            )
        ][:limit]

    def as_dict(self, item):
        return {
            "source_type": item.source_type,
            "source_id": item.source_id,
            "title": item.title,
            "media_type": item.media_type,
            "media_path": item.media_path,
            "media_role": item.media_role,
        }


def test_he_pronoun_reuses_selected_product_for_media() -> None:
    agent = BusinessAgent(_MediaStore())
    agent._remember_product("u1", "p4")
    decision = IntentDecision(
        intent=Intent.PRODUCT_MEDIA,
        confidence=0.99,
        reason="media",
        query_for_search="把他的视频发我",
        media_action=MediaAction.SEND,
        media_type="video",
        media_role="product_video",
    )

    reply = agent._answer_media(
        "u1",
        "把他的视频发我",
        "把他的视频发我",
        decision,
    )

    assert reply.media_action == MediaAction.SEND
    assert reply.sources[0]["source_id"] == "m4"


def test_media_without_product_keeps_send_action_for_model_follow_up() -> None:
    agent = BusinessAgent(SimpleNamespace(search_media=lambda **_: []))
    decision = IntentDecision(
        intent=Intent.PRODUCT_MEDIA,
        confidence=0.99,
        reason="media",
        query_for_search="把他的视频发我",
        media_action=MediaAction.SEND,
        media_type="video",
        media_role="product_video",
    )

    reply = agent._answer_media(
        "u2",
        "把他的视频发我",
        "把他的视频发我",
        decision,
    )

    assert "具体产品型号" in reply.content
    pending = agent._state_get(agent._pending_media_by_user, "u2")
    assert pending["media_action"] == str(MediaAction.SEND)
    assert pending["media_role"] == "product_video"


def test_media_clarification_then_model_continues_original_send_action() -> None:
    agent = BusinessAgent(_MediaStore())

    first = agent.answer("u3", "把他的视频发我")
    second = agent.answer("u3", "LY0653NBT-T")

    assert "具体产品型号" in first.content
    assert second.intent == Intent.PRODUCT_MEDIA
    assert second.media_action == MediaAction.SEND
    assert second.sources[0]["source_id"] == "m4"


def test_pending_media_only_consumes_bare_model_follow_up() -> None:
    assert _pending_media_is_model_only("LY0653NBT-T") is True
    assert _pending_media_is_model_only("发我0601价格在200到250之间的产品") is False
    assert _pending_media_is_model_only("发我0601产品图") is False
