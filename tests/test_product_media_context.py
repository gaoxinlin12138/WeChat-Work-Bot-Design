from types import SimpleNamespace

from app.business.graph.builder import (
    _apply_ordinal_media_context,
    _expand_compound_media_tasks,
    _pending_media_is_model_only,
)
from app.business.intent import Intent, IntentDecision, MediaAction, media_requests
from app.business.planner import PlannedTask, TaskPlan
from app.business.product_query import ProductQuery
from app.business.product_selection import is_product_follow_up
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


def test_media_requests_keeps_both_image_and_video() -> None:
    assert media_requests("把他的图片和视频发我") == (
        ("image", None),
        ("video", "product_video"),
    )


def test_media_pronoun_follow_up_keeps_selected_product_after_again_marker() -> None:
    assert is_product_follow_up("再把他的图片发我") is True


def test_compound_media_plan_is_expanded_into_two_tasks() -> None:
    decision = IntentDecision(
        intent=Intent.PRODUCT_MEDIA,
        confidence=0.99,
        reason="media",
        query_for_search="把他的图片和视频发我",
        media_action=MediaAction.SEND,
        media_type="video",
        media_role="product_video",
    )
    plan = TaskPlan(
        goal="把他的图片和视频发我",
        tasks=(PlannedTask(decision, ProductQuery(operation="media")),),
        confidence=0.99,
        reason="media",
        source="test",
    )

    expanded = _expand_compound_media_tasks(plan, "把他的图片和视频发我")

    assert len(expanded.tasks) == 2
    assert [(item.decision.media_type, item.decision.media_role) for item in expanded.tasks] == [
        ("image", None),
        ("video", "product_video"),
    ]


def test_ordinal_media_plan_injects_selected_model() -> None:
    decision = IntentDecision(
        intent=Intent.PRODUCT_MEDIA,
        confidence=0.8,
        reason="media",
        query_for_search="把第九个的图片发我",
        media_action=MediaAction.SEND,
        media_type="image",
    )
    plan = TaskPlan(
        goal="把第九个的图片发我",
        tasks=(PlannedTask(decision, ProductQuery(operation="media")),),
        confidence=0.8,
        reason="media",
        source="model",
        needs_clarification=True,
        clarification_question="请提供型号",
    )

    bound = _apply_ordinal_media_context(
        plan,
        query="把第九个的图片发我",
        selected=("p9", "LY0669GT-A"),
    )

    assert bound.needs_clarification is False
    task = bound.tasks[0]
    assert task.decision.needs_clarification is False
    assert "LY0669GT-A" in task.decision.query_for_search
    assert task.product_query is not None
    assert task.product_query.resolved_product_ids == ("p9",)
