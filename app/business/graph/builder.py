"""Build the plan-execute-verify LangGraph workflow for the business assistant."""

import re
import time
from dataclasses import replace
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.business import pending
from app.business.graph.reply_script import get_reply_script_graph
from app.business.graph.state import (
    BusinessGraphContext,
    BusinessGraphState,
    plan_from_payload,
    plan_to_payload,
    reply_from_payload,
    reply_to_payload,
)
from app.business.intent import (
    Intent,
    IntentDecision,
    MediaAction,
    ReplyScenario,
    fallback_intent_decision,
    is_media_send_follow_up,
    is_product_catalog_query,
)
from app.business.planner import PlannedTask, TaskPlan, plan_from_decision, plan_tasks
from app.business.product_focus import (
    ProductFocus,
    focus_from_dict,
    focus_from_legacy,
    ordinal_index,
)
from app.business.product_query import (
    ProductFieldRequest,
    ProductReference,
    product_query_from_dict,
)
from app.business.product_selection import has_product_identifier
from app.business.query_governance import OutputPolicy, enforce_output_budget
from app.business.query_plan import (
    PermissionContext,
    product_query_from_plan,
    query_plan_from_product_query,
    review_query_plan,
)
from app.business.response_style import normalize_wecom_answer
from app.business.tools.common import ReplySession, RoutedReply
from app.business.tools.reply_script import _NEW_TASK_MARKERS, extract_reply_fields
from app.messages import WELCOME_MESSAGE

PlanRoute = Literal["clarification", "execute"]
ExecutionRoute = Literal["execute", "verify"]


_COUNT_LISTING_FOLLOW_UP_MARKERS = (
    "分别是",
    "分别哪",
    "具体是",
    "具体哪",
    "哪几个",
    "哪几种",
    "哪几款",
    "哪两个",
    "哪两种",
    "哪两款",
    "把它们列出来",
    "把这些列出来",
    "列出来看看",
)

_FOLLOW_UP_FIELDS = (
    (("价格", "多少钱", "售价", "套装价"), "set_price", "价格"),
    (("主体价", "主体价格"), "body_price", "主体价格"),
    (("成本价", "采购价", "进价"), "cost_price", "成本价"),
    (("材质", "材料"), "material", "材质"),
    (("颜色",), "color", "颜色"),
    (("配件", "组成"), "components", "配件"),
)


def _ordinal_detail_follow_up_plan(query: str, product_context: object) -> TaskPlan | None:
    if isinstance(product_context, ProductFocus):
        focus = product_context
    else:
        focus = focus_from_dict(product_context)
        if focus is None or (
            not focus.result_ids and not focus.result_models and focus.query_plan is None
        ):
            focus = focus_from_legacy(product_context)
    if focus is None or not focus.is_valid():
        return None
    parsed = ordinal_index(query)
    if parsed is None:
        return None
    index, marker = parsed
    if marker == "后者" and len(focus.result_ids or focus.result_models) != 2:
        return None
    selected = focus.item_at(index)
    if selected is None:
        return None
    model_id, model = selected
    if not model_id:
        return None
    field = next(
        iter(
            sorted(
                (
                    (name, marker)
                    for markers, name, _evidence in _FOLLOW_UP_FIELDS
                    for marker in markers
                    if marker in query
                ),
                key=lambda item: len(item[1]),
                reverse=True,
            )
        ),
        None,
    )
    if field is None:
        return None
    reference_value = model or f"id:{model_id}"
    reference = ProductReference(
        reference_value,
        "exact" if model else "context",
        reference_value,
        "context",
    )
    # The product comes from the previous displayed result; the field is a
    # fresh request in the current message and must be grounded there.
    request = ProductFieldRequest(field[0], field[1], "current")
    from app.business.product_query import ProductQuery

    plan = ProductQuery(
        operation="detail",
        references=(reference,),
        include_models=(model,) if model else (),
        requested_fields=(request,),
        fields=(field[0],),
        context_mode="continuation",
        allow_overview=False,
        resolved_product_ids=(model_id,) if focus.result_ids else (),
    )
    decision = IntentDecision(
        intent=Intent.PRODUCT,
        confidence=0.99,
        reason="承接上一轮产品列表，查询指定序号产品信息",
        query_for_search=query,
        source="product_query_context",
    )
    return TaskPlan(
        goal=query,
        tasks=(PlannedTask(decision, plan),),
        confidence=decision.confidence,
        reason=decision.reason,
        source=decision.source,
    )


def _count_listing_follow_up_plan(query: str, product_context: object) -> TaskPlan | None:
    """Turn an ellipsis after a product count into the matching product list."""
    if isinstance(product_context, ProductFocus):
        focus = product_context
    else:
        focus = focus_from_dict(product_context)
        if focus is None or (
            not focus.result_ids and not focus.result_models and focus.query_plan is None
        ):
            focus = focus_from_legacy(product_context)
    if (
        focus is None
        or not focus.is_valid()
        or not any(marker in query for marker in _COUNT_LISTING_FOLLOW_UP_MARKERS)
    ):
        return None
    saved_query = product_query_from_dict(focus.query_plan)
    # Count replies now include the first product page immediately. Keep this
    # continuation path for users who still ask to see the list again, whether
    # the saved query came from the old count-only behavior or the new list-aware
    # count response.
    if saved_query is None or saved_query.operation not in {"count", "list"}:
        return None
    continued_query = replace(
        saved_query,
        operation="list",
        references=tuple(
            replace(item, source="context") for item in saved_query.effective_references()
        ),
        requested_fields=(),
        fields=(),
        original_fields=(),
        filters=tuple(replace(item, source="context") for item in saved_query.filters),
        aggregate=None,
        page=1,
        page_size=5,
        context_mode="continuation",
        allow_overview=True,
        required_operation=None,
    )
    decision = IntentDecision(
        intent=Intent.PRODUCT,
        confidence=0.99,
        reason="承接上一轮产品数量查询，列出对应产品",
        query_for_search=query,
        source="product_query_context",
    )
    return TaskPlan(
        goal=query,
        tasks=(PlannedTask(decision, continued_query),),
        confidence=decision.confidence,
        reason=decision.reason,
        source=decision.source,
    )


def normalize_input(state: BusinessGraphState) -> dict[str, object]:
    """Start every invocation with fresh execution state, including checkpoints."""
    return {
        "normalized_query": " ".join(state["query"].split())[:1000],
        "task_index": 0,
        "task_replies": [],
    }


def _planner_client(settings):
    from app.business import service

    if not (
        getattr(settings, "task_planner_enabled", True)
        and getattr(settings, "llm_api_key", None)
        and getattr(settings, "llm_model", None)
    ):
        return None
    try:
        return service.get_rag_agent()._llm_client()
    except Exception:
        return None


def _product_context_decision(
    agent,
    user_id: str,
    query: str,
    fallback: IntentDecision,
    *,
    model_available: bool = True,
) -> IntentDecision | None:
    # 上一轮歧义的接回已统一由 pending 槽位在 create_plan 开头处理，
    # 这里不再重复一套「读 V2 上下文的 pending_ambiguity」的分支。
    decision = agent._resume_product_choice(user_id, query, fallback)
    if decision is not None:
        return decision
    if agent._catalog_page_requested(user_id, query):
        return replace(
            fallback,
            intent=Intent.PRODUCT,
            needs_clarification=False,
            clarification_question="",
            query_for_search=query,
            confidence=max(fallback.confidence, 0.95),
            reason="继续查看上一轮产品目录",
            source="conversation_state",
        )
    # 这里不再拦截 `is_product_catalog_query`——但**只在有模型判断时才放开**。
    #
    # 以前只要判定成「盘点产品」（有哪些产品、什么颜色、哪个便宜）就直接用确定性模板
    # 建计划，模型根本不问。模板只认识写死的那几个词，而这类提问真正需要的是**内容理解**：
    # 把「灰玻璃·枪灰颜色的产品有哪些」里的颜色取成筛选条件。模板做不到——它把颜色
    # 当成「要显示的字段」，于是回了全库 61 款而不是该颜色的 19 款；而模型本来就会输出
    # filters。
    #
    # 但无模型时模板仍是有价值的兜底（拿掉它会让无模型路径的清单类提问变差，
    # 实测计划匹配率 96.1% → 94.7%），所以保留为**模型不可用时的降级路径**。
    if not model_available and is_product_catalog_query(query):
        return replace(
            fallback,
            intent=Intent.PRODUCT,
            needs_clarification=False,
            clarification_question="",
            query_for_search=query,
            confidence=max(fallback.confidence, 0.95),
            reason="问题是在盘点当前产品，统一使用产品清单模板（无模型时的兜底）",
            source="deterministic_product_template",
        )
    if (
        fallback.intent in {Intent.GENERAL, Intent.CONVERSATION}
        and agent._state_get(agent._last_product_by_user, user_id)
        and not has_product_identifier(query)
        and len(query) <= 30
        and any(word in query for word in ("那个", "那款", "这款", "它", "换成"))
        and not any(
            word in query
            for word in ("报告", "证书", "执照", "简介", "品牌", "公司资料", "注册地址")
        )
    ):
        return replace(
            fallback,
            intent=Intent.PRODUCT,
            needs_clarification=False,
            clarification_question="",
            source="conversation_state",
            reason="结合已选择的产品型号查询其他产品",
        )
    return None


def _apply_session_context(
    plan: TaskPlan,
    *,
    agent,
    user_id: str,
    query: str,
    active_session: ReplySession | None,
) -> TaskPlan:
    if active_session is None:
        return plan
    is_supplement = bool(
        extract_reply_fields(query, active_session.scenario)
        and not any(marker in query for marker in _NEW_TASK_MARKERS)
    )
    if is_supplement:
        return plan_from_decision(
            IntentDecision(
                intent=Intent.REPLY_SCRIPT,
                confidence=max(plan.confidence, 0.9),
                reason="结合未完成的话术任务，正在补充客户情况",
                query_for_search=query,
                source="conversation_state",
                reply_scenario=active_session.scenario,
            ),
            query,
        )

    updated_tasks: list[PlannedTask] = []
    has_reply_task = False
    for task in plan.tasks:
        decision = task.decision
        if decision.intent == Intent.REPLY_SCRIPT:
            has_reply_task = True
            scenario = decision.reply_scenario
            if scenario in (None, ReplyScenario.UNKNOWN):
                scenario = active_session.scenario
            decision = replace(decision, reply_scenario=scenario)
        updated_tasks.append(PlannedTask(decision, task.product_query))

    if not has_reply_task:
        sole = plan.tasks[0].decision if len(plan.tasks) == 1 else None
        if (
            sole is not None
            and sole.source == "rules"
            and (
                sole.intent in {Intent.GENERAL, Intent.CONVERSATION}
                or (
                    extract_reply_fields(query, active_session.scenario)
                    and not any(
                        marker in query
                        for marker in (
                            "是什么",
                            "有哪些",
                            "多少",
                            "怎么规定",
                            "介绍一下",
                            "查一下",
                            "图片",
                            "视频",
                            "发我",
                        )
                    )
                )
            )
        ):
            return plan_from_decision(
                IntentDecision(
                    intent=Intent.REPLY_SCRIPT,
                    confidence=0.9,
                    reason="正在继续尚未完成的客户回复任务",
                    query_for_search=query,
                    source="conversation_state",
                    reply_scenario=active_session.scenario,
                ),
                query,
            )
        agent._state_pop(agent._reply_sessions, user_id)
    return replace(plan, tasks=tuple(updated_tasks))


def _apply_media_context(
    plan: TaskPlan,
    *,
    agent,
    user_id: str,
    query: str,
) -> TaskPlan:
    if len(plan.tasks) != 1:
        return plan
    decision = plan.tasks[0].decision
    if decision.intent not in {Intent.GENERAL, Intent.CONVERSATION}:
        return plan
    if not agent._state_contains(agent._last_media_by_user, user_id):
        return plan
    if not is_media_send_follow_up(query):
        return plan
    media_context = agent._state_get(agent._last_media_by_user, user_id)
    if not isinstance(media_context, (list, tuple)) or len(media_context) != 2:
        return plan
    media_type, media_role = media_context
    return plan_from_decision(
        replace(
            decision,
            intent=Intent.PRODUCT_MEDIA,
            confidence=max(decision.confidence, 0.98),
            reason="结合上一轮媒体列表，员工正在要求发送媒体",
            source="conversation_state",
            media_action=MediaAction.SEND,
            media_type=media_type,
            media_role=media_role,
            needs_clarification=False,
            clarification_question="",
        ),
        query,
    )


def _prefetch_embedding(plan: TaskPlan, query: str, runtime: Runtime[BusinessGraphContext]) -> None:
    from app.business import service

    settings = service.get_settings()
    if len(plan.tasks) != 1:
        return
    decision = plan.tasks[0].decision
    has_contextual_reference = any(
        marker in query for marker in ("这个", "那个", "它", "上面", "刚才", "还有呢", "那就")
    )
    if (
        decision.intent in {Intent.POLICY, Intent.PRODUCT, Intent.GENERAL}
        and not has_contextual_reference
        and decision.query_for_search == query
        and getattr(settings, "embedding_api_key", None)
        and getattr(settings, "embedding_model", None)
    ):
        runtime.context.embedding_future = service._ROUTING_EXECUTOR.submit(
            service.get_rag_agent().embed, [query]
        )


def _apply_unified_query_plan(
    plan: TaskPlan,
    *,
    message: str,
    user_id: str,
    can_view_sensitive: bool,
    enabled: bool,
) -> TaskPlan:
    """Validate product tasks through QueryPlan before the V2 executor.

    ProductQuery remains the sole executor. The adapter only makes the
    reviewed unified contract authoritative and preserves trusted graph-only
    metadata when it bridges back to ProductQuery. A failed review leaves the
    original V2 task untouched as a safe rollback path.
    """
    if not enabled:
        return plan
    permission = PermissionContext(
        user_id,
        frozenset({"product.cost.read"}) if can_view_sensitive else frozenset(),
    )
    tasks: list[PlannedTask] = []
    changed = False
    for task in plan.tasks:
        if task.product_query is None or task.decision.intent not in {
            Intent.PRODUCT,
            Intent.PRODUCT_MEDIA,
        }:
            tasks.append(task)
            continue
        structured = query_plan_from_product_query(
            task.product_query,
            domain="media" if task.decision.intent == Intent.PRODUCT_MEDIA else "product",
        )
        review = review_query_plan(structured, message, permission=permission)
        if not review.passed:
            tasks.append(task)
            continue
        reviewed = review.corrected_plan or structured
        bridged, validation = product_query_from_plan(
            reviewed,
            message,
            can_view_sensitive=can_view_sensitive,
            base_query=task.product_query,
        )
        if bridged is None or not validation.passed:
            tasks.append(task)
            continue
        tasks.append(replace(task, product_query=bridged))
        changed = changed or bridged != task.product_query
    if not changed:
        return plan
    return replace(plan, tasks=tuple(tasks))


def create_plan(
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    """Create the single authoritative execution plan for this message."""
    from app.business import service

    agent = runtime.context.agent
    user_id = runtime.context.user_id
    query = state["normalized_query"]
    settings = service.get_settings()
    # 如果本句是在回答上一轮的澄清问题，直接用接回的计划，不再重新理解一遍。
    resolved = pending.resolve(agent, user_id, query)
    if resolved is not None:
        return {"plan": plan_to_payload(resolved)}
    recent_context = agent._recent_intent_context(user_id)
    active_session = agent._state_get(agent._reply_sessions, user_id)
    if active_session is not None and not isinstance(active_session, ReplySession):
        active_session = None

    stateless_fallback = fallback_intent_decision(query, [])
    continuation = bool(
        agent._catalog_page_requested(user_id, query)
        or any(
            word in query
            for word in (
                "它",
                "这个",
                "那个",
                "这款",
                "第二个",
                "下一页",
                "上一页",
                "发给我",
            )
        )
    )
    product_context = agent._get_product_focus(user_id)
    ordinal_follow_up = _ordinal_detail_follow_up_plan(query, product_context)
    if ordinal_follow_up is not None:
        return {"plan": plan_to_payload(ordinal_follow_up)}
    count_follow_up = _count_listing_follow_up_plan(query, product_context)
    if count_follow_up is not None:
        return {"plan": plan_to_payload(count_follow_up)}
    if continuation and product_context is not None:
        planner_context = [
            {
                "role": "system",
                "content": "上一轮已验证的产品查询上下文："
                + str(
                    product_context.query_plan if isinstance(product_context, ProductFocus) else {}
                )[:1500],
                "intent": str(Intent.PRODUCT),
            }
        ]
    elif stateless_fallback.intent in {
        Intent.PRODUCT,
        Intent.PRODUCT_MEDIA,
    } or has_product_identifier(query):
        planner_context = []
    else:
        planner_context = recent_context

    fallback = fallback_intent_decision(query, planner_context)
    planner_client = _planner_client(settings)
    contextual = _product_context_decision(
        agent, user_id, query, fallback, model_available=planner_client is not None
    )
    if contextual is not None:
        plan = plan_from_decision(contextual, query)
    else:
        routing_context = list(planner_context)
        if active_session is not None:
            routing_context.append(
                {
                    "role": "system",
                    "content": "当前有未完成的客户回复任务；判断本条消息是补充任务还是切换目标。",
                    "intent": str(Intent.REPLY_SCRIPT),
                }
            )
        plan = plan_tasks(
            query,
            recent_context=routing_context,
            client=planner_client,
            model=getattr(settings, "llm_model", None),
            confidence_threshold=getattr(settings, "intent_confidence_threshold", 0.45),
            decision_resolver=service.decide_intent,
        )

    plan = _apply_session_context(
        plan,
        agent=agent,
        user_id=user_id,
        query=query,
        active_session=active_session,
    )
    plan = _apply_media_context(plan, agent=agent, user_id=user_id, query=query)
    plan = _apply_unified_query_plan(
        plan,
        message=query,
        user_id=user_id,
        can_view_sensitive=runtime.context.can_view_cost,
        enabled=getattr(settings, "unified_query_plan_enabled", True),
    )

    intents = {task.decision.intent for task in plan.tasks}
    if not intents.intersection({Intent.PRODUCT, Intent.PRODUCT_MEDIA}) and intents.intersection(
        {Intent.POLICY, Intent.GENERAL, Intent.REPLY_SCRIPT}
    ):
        agent._clear_product_context(user_id)
    _prefetch_embedding(plan, query, runtime)
    return {"plan": plan_to_payload(plan)}


def choose_after_plan(state: BusinessGraphState) -> PlanRoute:
    return "clarification" if plan_from_payload(state["plan"]).needs_clarification else "execute"


def clarification_node(
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    plan = plan_from_payload(state["plan"])
    # 澄清分支不经过 execute_task，如果不在这里记录，本轮已解析出的型号和字段就丢了，
    # 员工回答之后系统只会重新索要型号。
    pending.remember(runtime.context.agent, runtime.context.user_id, plan)
    reply = RoutedReply(
        plan.primary_intent,
        plan.clarification_question or "为了避免答错，请补充你希望我优先完成的任务。",
        [],
    )
    return {"reply": reply_to_payload(reply)}


def _prefetched_embedding(
    task: PlannedTask,
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> list[float] | None:
    from app.business import service

    future = runtime.context.embedding_future
    decision = task.decision
    if (
        future is None
        or decision.query_for_search != state["normalized_query"]
        or decision.intent not in {Intent.POLICY, Intent.PRODUCT, Intent.GENERAL}
    ):
        return None
    try:
        return future.result()[0]
    except Exception as exc:
        service.logger.warning("planner_embedding_future_failed", error_type=type(exc).__name__)
        return None


def _domain_kwargs(
    task: PlannedTask,
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    decision = task.decision
    context = runtime.context
    return {
        "user_id": context.user_id,
        # Evidence must be checked against the employee's raw message. The
        # planner may normalize search terms, but it is not the source quote.
        "original_query": state["normalized_query"],
        "search_query": decision.query_for_search,
        "decision": decision,
        "product_query": task.product_query,
        # Structured product contract: downstream tools receive the validated
        # operation and filters explicitly, alongside the versioned query.
        "operation": (task.product_query.operation if task.product_query is not None else None),
        "filters": (task.product_query.filters if task.product_query is not None else ()),
        "prefetched_embedding": _prefetched_embedding(task, state, runtime),
        "stream_callback": context.stream_callback,
        "can_view_cost": context.can_view_cost,
        "cost_access_message": context.cost_access_message,
    }


def _reply_task_query(state: BusinessGraphState, decision: IntentDecision) -> str:
    previous = []
    for item in state.get("task_replies", []):
        content = reply_from_payload(item).content
        safe_lines = [
            line
            for line in content.splitlines()
            if not any(marker in line for marker in ("成本价", "采购价"))
        ]
        safe_content = "\n".join(safe_lines).strip()
        if safe_content:
            previous.append(safe_content)
    if not previous:
        return decision.query_for_search
    evidence = "\n".join(item[:1000] for item in previous if item.strip())[:2500]
    return f"{decision.query_for_search}\n已查询到的业务资料：\n{evidence}"


def execute_task(
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    """Execute exactly one validated task; the graph loop owns sequencing."""
    plan = plan_from_payload(state["plan"])
    index = state.get("task_index", 0)
    task = plan.tasks[index]
    decision = task.decision
    agent = runtime.context.agent

    if decision.intent == Intent.CONVERSATION:
        reply = RoutedReply(decision.intent, WELCOME_MESSAGE, [])
    elif decision.intent in {Intent.POLICY, Intent.GENERAL}:
        reply = agent.tools.knowledge.answer(**_domain_kwargs(task, state, runtime))
    elif decision.intent == Intent.PRODUCT:
        reply = agent.tools.product.answer(**_domain_kwargs(task, state, runtime))
    elif decision.intent == Intent.PRODUCT_MEDIA:
        reply = agent.tools.media.answer(**_domain_kwargs(task, state, runtime))
    elif decision.intent == Intent.REPLY_SCRIPT:
        result = get_reply_script_graph().invoke(
            {
                "query": _reply_task_query(state, decision),
                "semantic_scenario": (
                    str(decision.reply_scenario) if decision.reply_scenario is not None else None
                ),
            },
            context=runtime.context,
        )
        reply = reply_from_payload(result["reply"])
    else:
        reply = RoutedReply(decision.intent, "暂时无法执行这个业务任务。", [])

    replies = [*state.get("task_replies", []), reply_to_payload(reply)]
    return {"task_replies": replies, "task_index": index + 1}


def choose_after_execution(state: BusinessGraphState) -> ExecutionRoute:
    plan = plan_from_payload(state["plan"])
    return "execute" if state.get("task_index", 0) < len(plan.tasks) else "verify"


_SECTION_LABELS = {
    Intent.POLICY: "制度依据",
    Intent.GENERAL: "资料结果",
    Intent.PRODUCT: "产品信息",
    Intent.PRODUCT_MEDIA: "产品媒体",
    Intent.REPLY_SCRIPT: "客户回复",
    Intent.CONVERSATION: "回复",
}


def _deduplicate_sources(replies: list[RoutedReply]) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for reply in replies:
        for source in reply.sources:
            key = (
                source.get("source_type"),
                source.get("source_id") or source.get("id"),
                source.get("media_path"),
                source.get("page_number"),
                source.get("chunk_index"),
            )
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
    return sources


def _with_inventory_assessment(query: str, replies: list[RoutedReply]) -> list[RoutedReply]:
    requirement = re.search(r"够不够\s*(\d+)\s*(?:套|件|个|台)?", query)
    if requirement is None:
        requirement = re.search(r"(?:需要|要|采购)\s*(\d+)\s*(?:套|件|个|台)", query)
    if requirement is None:
        return replies
    required = int(requirement.group(1))
    updated: list[RoutedReply] = []
    for reply in replies:
        if reply.intent != Intent.PRODUCT:
            updated.append(reply)
            continue
        available_match = re.search(r"(?:件数|库存)：\s*(\d+)\s*件", reply.content)
        if available_match is None:
            updated.append(reply)
            continue
        available = int(available_match.group(1))
        if available >= required:
            assessment = (
                f"按当前登记件数计算，可满足{required}套需求，预计余量{available - required}件。"
            )
        else:
            assessment = f"按当前登记件数计算，不足{required}套，还差{required - available}件。"
        updated.append(replace(reply, content=f"{reply.content}\n{assessment}"))
    return updated


def verify_results(state: BusinessGraphState) -> dict[str, object]:
    """Verify completeness and merge observations without inventing new facts."""
    plan = plan_from_payload(state["plan"])
    replies = [reply_from_payload(item) for item in state.get("task_replies", [])]
    replies = _with_inventory_assessment(state["normalized_query"], replies)
    if not replies:
        reply = RoutedReply(plan.primary_intent, "任务没有产生可用结果，请稍后重试。", [])
        return {"reply": reply_to_payload(reply)}
    if len(replies) == 1:
        return {"reply": reply_to_payload(replies[0])}

    sections = [
        f"【{_SECTION_LABELS.get(reply.intent, '任务结果')}】\n{reply.content.strip()}"
        for reply in replies
        if reply.content.strip()
    ]
    media_reply = next(
        (
            reply
            for reply in replies
            if reply.media_action == MediaAction.SEND
            and (
                reply.intent == Intent.PRODUCT_MEDIA
                or any(source.get("media_type") == "file" for source in reply.sources)
            )
        ),
        None,
    )
    primary = media_reply or replies[-1]
    missing_fields = list(
        dict.fromkeys(field for reply in replies for field in reply.missing_fields)
    )
    risks = list(dict.fromkeys(risk for reply in replies for risk in reply.risk_warnings))
    merged = RoutedReply(
        intent=Intent.PRODUCT_MEDIA if media_reply else plan.primary_intent,
        content="\n\n".join(sections) or "任务已执行，但没有可展示内容。",
        sources=_deduplicate_sources(replies),
        media_action=media_reply.media_action if media_reply else primary.media_action,
        scenario=next((reply.scenario for reply in reversed(replies) if reply.scenario), None),
        missing_fields=missing_fields,
        risk_warnings=risks,
        execution_trace=next(
            (reply.execution_trace for reply in replies if reply.execution_trace), {}
        ),
    )
    return {"reply": reply_to_payload(merged)}


def finalize_reply(
    state: BusinessGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    from app.business import service

    agent = runtime.context.agent
    plan = plan_from_payload(state["plan"])
    base_reply = reply_from_payload(state["reply"])
    bounded_content, truncated, truncation_reason = enforce_output_budget(
        normalize_wecom_answer(base_reply.content), policy=OutputPolicy()
    )
    execution_trace = dict(base_reply.execution_trace or {})
    execution_trace["output"] = {
        "character_limit": OutputPolicy().max_chars,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    }
    # Keep the canonical contract and review in the trace. The established
    # ProductQuery executor remains the source of truth; no model output is sent
    # directly to a tool.
    unified_plans = []
    unified_reviews = []
    for task in plan.tasks:
        if task.product_query is None:
            continue
        structured = query_plan_from_product_query(task.product_query)
        review = review_query_plan(
            structured,
            state["normalized_query"],
            permission=PermissionContext(
                runtime.context.user_id,
                frozenset({"product.cost.read"}) if runtime.context.can_view_cost else frozenset(),
            ),
        )
        unified_plans.append(structured.to_dict())
        unified_reviews.append(review.to_dict())
    if unified_plans:
        execution_trace["unified_query_plans"] = unified_plans
        execution_trace["query_plan_reviews"] = unified_reviews
    reply = replace(
        base_reply,
        content=bounded_content,
        intent_confidence=plan.confidence,
        intent_reason=plan.reason,
        understood_query=plan.goal,
        intent_source=plan.source,
        execution_trace=execution_trace,
    )
    agent._record_intent_turn(
        runtime.context.user_id,
        state["normalized_query"],
        reply,
        request_id=state.get("request_id"),
    )
    try:
        agent.tools.audit.record_audit(
            user_id=runtime.context.user_id,
            query=state["normalized_query"],
            reply=reply,
            response_ms=(time.perf_counter() - runtime.context.started_at) * 1000,
            request_id=state.get("request_id"),
        )
    except Exception as exc:
        service.logger.warning("intent_audit_write_failed", error_type=type(exc).__name__)
    return {"reply": reply_to_payload(reply)}


def build_business_graph(checkpointer=None):
    graph = StateGraph(BusinessGraphState, context_schema=BusinessGraphContext)
    graph.add_node("normalize_input", normalize_input)
    graph.add_node("create_plan", create_plan)
    graph.add_node("clarification", clarification_node)
    graph.add_node("execute_task", execute_task)
    graph.add_node("verify_results", verify_results)
    graph.add_node("finalize_reply", finalize_reply)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "create_plan")
    graph.add_conditional_edges(
        "create_plan",
        choose_after_plan,
        {"clarification": "clarification", "execute": "execute_task"},
    )
    graph.add_edge("clarification", "finalize_reply")
    graph.add_conditional_edges(
        "execute_task",
        choose_after_execution,
        {"execute": "execute_task", "verify": "verify_results"},
    )
    graph.add_edge("verify_results", "finalize_reply")
    graph.add_edge("finalize_reply", END)
    return graph.compile(checkpointer=checkpointer, name="business-assistant")
