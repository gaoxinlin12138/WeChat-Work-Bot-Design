from app.business.canonical import CanonicalRequest, canonicalize_request
from app.business.graph.builder import _expand_compound_media_tasks
from app.business.intent import Intent, IntentDecision, MediaAction
from app.business.planner import PlannedTask, TaskPlan
from app.business.product_focus import ProductFocus
from app.business.product_query import ProductFieldRequest, ProductQuery, ProductReference


def _plan(
    query: str, decision: IntentDecision, product_query: ProductQuery | None = None
) -> TaskPlan:
    return TaskPlan(query, (PlannedTask(decision, product_query),), 0.9, "test", "test")


def test_canonical_contract_roundtrip_is_stable_and_auditable() -> None:
    query = "LY0653GDT-T 的详细信息"
    decision = IntentDecision(Intent.PRODUCT, 0.9, "detail", query)
    product_query = ProductQuery(
        operation="detail",
        references=(ProductReference("LY0653GDT-T", "exact", "LY0653GDT-T"),),
        requested_fields=(ProductFieldRequest("material", "材质"),),
    )

    request = canonicalize_request(query, _plan(query, decision, product_query))

    assert isinstance(request, CanonicalRequest)
    assert request.tasks[0].product_query is not None
    assert request.tasks[0].product_query.allow_overview is True
    assert request.tasks[0].product_query.requested_fields == ()
    assert request.tasks[0].audit["overview_semantics"] is True
    assert (
        request.fingerprint
        == canonicalize_request(query, _plan(query, decision, product_query)).fingerprint
    )
    restored = CanonicalRequest.from_json(request.json())
    assert restored.to_dict(include_fingerprint=False) == request.to_dict(include_fingerprint=False)


def test_ordinal_and_pronoun_resolution_never_guess_without_focus() -> None:
    decision = IntentDecision(
        Intent.PRODUCT,
        0.9,
        "detail",
        "第2个的材质",
        needs_clarification=True,
        clarification_question="请提供型号",
    )
    query = ProductQuery(operation="detail", context_mode="continuation")
    unresolved = canonicalize_request("第2个的材质", _plan("第2个的材质", decision, query))
    assert unresolved.needs_clarification is True
    assert unresolved.unresolved == ("第2个",)
    assert unresolved.tasks[0].product_query.include_models == ()

    focus = ProductFocus(result_ids=("p2",), result_models=("LY0669GT-A",), expires_at=9999999999)
    resolved = canonicalize_request("它的材质", _plan("它的材质", decision, query), focus=focus)
    assert resolved.needs_clarification is False
    assert resolved.tasks[0].clarification_question == ""
    assert resolved.tasks[0].product_query.resolved_product_ids == ("p2",)
    assert resolved.tasks[0].product_query.include_models == ("LY0669GT-A",)


def test_catalog_overview_is_a_valid_unscoped_list() -> None:
    query = "现在都有什么产品"
    decision = IntentDecision(Intent.PRODUCT, 0.9, "catalog", query)
    request = canonicalize_request(
        query,
        _plan(query, decision, ProductQuery(operation="list")),
    )
    assert request.needs_clarification is False
    assert request.tasks[0].product_query.allow_overview is True


def test_canonical_media_request_fans_out_image_and_video() -> None:
    query = "把 LY0653GDT-T 的图片和视频发我"
    decision = IntentDecision(
        Intent.PRODUCT_MEDIA,
        0.9,
        "media",
        query,
        media_action=MediaAction.SEND,
        media_type="image",
    )
    product_query = ProductQuery(operation="media", include_models=("LY0653GDT-T",))

    request = canonicalize_request(query, _plan(query, decision, product_query))

    assert [(task.decision.media_type, task.decision.media_role) for task in request.tasks] == [
        ("image", None),
        ("video", "product_video"),
    ]
    assert all(task.product_query.include_models == ("LY0653GDT-T",) for task in request.tasks)

    pre_split = _expand_compound_media_tasks(_plan(query, decision, product_query), query)
    assert len(canonicalize_request(query, pre_split).tasks) == 2


def test_trusted_focus_overrides_model_missing_model_clarification() -> None:
    query = "把它的图片和视频都发我"
    decision = IntentDecision(
        Intent.PRODUCT_MEDIA,
        0.6,
        "模型未确认对象",
        query,
        needs_clarification=True,
        clarification_question="请提供具体产品型号",
        media_action=MediaAction.SEND,
        media_type="video",
        media_role="product_video",
    )
    focus = ProductFocus(
        result_ids=("p5",), result_models=("LY0669GT-A",), expires_at=9999999999
    )
    request = canonicalize_request(
        query,
        _plan(query, decision, ProductQuery(operation="media", context_mode="continuation")),
        focus=focus,
    )
    assert request.needs_clarification is False
    assert request.tasks[0].product_query.resolved_product_ids == ("p5",)
