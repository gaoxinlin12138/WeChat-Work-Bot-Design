"""Serializable state and non-persistent runtime context for the business graph."""

import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.business.intent import Intent, IntentDecision, MediaAction, ReplyScenario
from app.business.planner import PlannedTask, ProductFilter, ProductQuery, TaskPlan
from app.business.product_query import (
    ProductAmbiguity,
    ProductFieldRequest,
    ProductReference,
    ProductSort,
)
from app.business.tools.common import ReplySession, RoutedReply


class RoutedReplyPayload(TypedDict):
    intent: str
    content: str
    sources: list[dict[str, object]]
    media_action: str | None
    scenario: str | None
    missing_fields: list[str]
    risk_warnings: list[str]
    intent_confidence: float
    intent_reason: str
    understood_query: str
    intent_source: str
    execution_trace: dict[str, object]


class IntentDecisionPayload(TypedDict):
    intent: str
    confidence: float
    reason: str
    query_for_search: str
    source: str
    needs_clarification: bool
    clarification_question: str
    media_action: str | None
    media_type: str | None
    media_role: str | None
    reply_scenario: str | None


class ReplySessionPayload(TypedDict):
    scenario: str
    collected: dict[str, str]
    last_seen: float


class TaskPlanPayload(TypedDict):
    goal: str
    tasks: list[dict[str, object]]
    confidence: float
    reason: str
    source: str
    needs_clarification: bool
    clarification_question: str


class BusinessGraphState(TypedDict, total=False):
    """Only checkpoint-safe values belong in this state."""

    request_id: str
    query: str
    normalized_query: str
    plan: TaskPlanPayload
    task_index: int
    task_replies: list[RoutedReplyPayload]
    reply: RoutedReplyPayload


@dataclass
class BusinessGraphContext:
    """Per-invocation objects that must never be persisted in graph state."""

    agent: Any
    user_id: str
    stream_callback: Callable[[str], None] | None = None
    can_view_cost: bool = False
    cost_access_message: str = "你当前没有查看成本价和采购价的权限。"
    started_at: float = field(default_factory=time.perf_counter)
    embedding_future: Future[list[list[float]]] | None = field(default=None, repr=False)


def decision_to_payload(decision: IntentDecision) -> IntentDecisionPayload:
    return {
        "intent": str(decision.intent),
        "confidence": decision.confidence,
        "reason": decision.reason,
        "query_for_search": decision.query_for_search,
        "source": decision.source,
        "needs_clarification": decision.needs_clarification,
        "clarification_question": decision.clarification_question,
        "media_action": (
            str(decision.media_action) if decision.media_action is not None else None
        ),
        "media_type": decision.media_type,
        "media_role": decision.media_role,
        "reply_scenario": (
            str(decision.reply_scenario) if decision.reply_scenario is not None else None
        ),
    }


def decision_from_payload(payload: IntentDecisionPayload) -> IntentDecision:
    return IntentDecision(
        intent=Intent(payload["intent"]),
        confidence=payload["confidence"],
        reason=payload["reason"],
        query_for_search=payload["query_for_search"],
        source=payload["source"],
        needs_clarification=payload["needs_clarification"],
        clarification_question=payload["clarification_question"],
        media_action=_optional_enum(MediaAction, payload["media_action"]),
        media_type=payload["media_type"],
        media_role=payload["media_role"],
        reply_scenario=_optional_enum(ReplyScenario, payload["reply_scenario"]),
    )


def plan_to_payload(plan: TaskPlan) -> TaskPlanPayload:
    return {
        "goal": plan.goal,
        "tasks": [
            {
                "decision": decision_to_payload(task.decision),
                "product_query": (
                    {
                        "operation": task.product_query.operation,
                        "version": task.product_query.version,
                        "include_models": list(task.product_query.include_models),
                        "exclude_models": list(task.product_query.exclude_models),
                        "fields": list(task.product_query.fields),
                        "filters": [
                            {
                                "field": item.field,
                                "operator": item.operator,
                                "value": item.value,
                                "evidence": item.evidence,
                                "source": item.source,
                            }
                            for item in task.product_query.filters
                        ],
                        "references": [
                            {
                                "value": item.value,
                                "kind": item.kind,
                                "evidence": item.evidence,
                                "source": item.source,
                            }
                            for item in task.product_query.references
                        ],
                        "requested_fields": [
                            {
                                "field": item.field,
                                "evidence": item.evidence,
                                "source": item.source,
                            }
                            for item in task.product_query.requested_fields
                        ],
                        "result_scope": task.product_query.result_scope,
                        "sorts": [
                            {"field": item.field, "direction": item.direction}
                            for item in task.product_query.sorts
                        ],
                        "page": task.product_query.page,
                        "page_size": task.product_query.page_size,
                        "context_mode": task.product_query.context_mode,
                        "ambiguities": [
                            {
                                "code": item.code,
                                "question": item.question,
                                "options": list(item.options),
                            }
                            for item in task.product_query.ambiguities
                        ],
                    }
                    if task.product_query is not None
                    else None
                ),
            }
            for task in plan.tasks
        ],
        "confidence": plan.confidence,
        "reason": plan.reason,
        "source": plan.source,
        "needs_clarification": plan.needs_clarification,
        "clarification_question": plan.clarification_question,
    }


def plan_from_payload(payload: TaskPlanPayload) -> TaskPlan:
    def task_from_payload(item: dict[str, object]) -> PlannedTask:
        # Read the pre-structured shape defensively so in-flight checkpoints from
        # the previous release can still complete after deployment.
        if "decision" not in item:
            return PlannedTask(decision_from_payload(item))  # type: ignore[arg-type]
        decision = decision_from_payload(item["decision"])  # type: ignore[arg-type]
        raw_query = item.get("product_query")
        if not isinstance(raw_query, dict):
            return PlannedTask(decision)
        filters = tuple(
            ProductFilter(
                field=str(raw["field"]),
                operator=str(raw["operator"]),  # type: ignore[arg-type]
                value=raw["value"],
                evidence=str(raw.get("evidence") or ""),
                source=str(raw.get("source") or "fallback"),  # type: ignore[arg-type]
            )
            for raw in raw_query.get("filters", [])
            if isinstance(raw, dict)
            and all(key in raw for key in ("field", "operator", "value"))
        )
        references = tuple(
            ProductReference(
                value=str(raw["value"]),
                kind=str(raw.get("kind") or "fragment"),  # type: ignore[arg-type]
                evidence=str(raw.get("evidence") or ""),
                source=str(raw.get("source") or "fallback"),  # type: ignore[arg-type]
            )
            for raw in raw_query.get("references", [])
            if isinstance(raw, dict) and raw.get("value")
        )
        requests = tuple(
            ProductFieldRequest(
                field=str(raw["field"]),
                evidence=str(raw.get("evidence") or ""),
                source=str(raw.get("source") or "fallback"),  # type: ignore[arg-type]
            )
            for raw in raw_query.get("requested_fields", [])
            if isinstance(raw, dict) and raw.get("field")
        )
        sorts = tuple(
            ProductSort(
                field=str(raw["field"]),
                direction=str(raw.get("direction") or "asc"),  # type: ignore[arg-type]
            )
            for raw in raw_query.get("sorts", [])
            if isinstance(raw, dict) and raw.get("field")
        )
        ambiguities = tuple(
            ProductAmbiguity(
                code=str(raw.get("code") or "product_ambiguity"),
                question=str(raw.get("question") or ""),
                options=tuple(str(item) for item in raw.get("options", [])),
            )
            for raw in raw_query.get("ambiguities", [])
            if isinstance(raw, dict) and raw.get("question")
        )
        return PlannedTask(
            decision,
            ProductQuery(
                operation=str(raw_query.get("operation", "detail")),  # type: ignore[arg-type]
                include_models=tuple(str(value) for value in raw_query.get("include_models", [])),
                exclude_models=tuple(str(value) for value in raw_query.get("exclude_models", [])),
                fields=tuple(str(value) for value in raw_query.get("fields", [])),
                filters=filters,
                version=int(raw_query.get("version") or 1),
                references=references,
                requested_fields=requests,
                result_scope=str(raw_query.get("result_scope") or "all_matches"),  # type: ignore[arg-type]
                sorts=sorts,
                page=max(1, int(raw_query.get("page") or 1)),
                page_size=max(1, int(raw_query.get("page_size") or 5)),
                context_mode=str(raw_query.get("context_mode") or "independent"),  # type: ignore[arg-type]
                ambiguities=ambiguities,
            ),
        )

    return TaskPlan(
        goal=payload["goal"],
        tasks=tuple(task_from_payload(item) for item in payload["tasks"]),
        confidence=payload["confidence"],
        reason=payload["reason"],
        source=payload["source"],
        needs_clarification=payload["needs_clarification"],
        clarification_question=payload["clarification_question"],
    )


def session_to_payload(session: ReplySession | None) -> ReplySessionPayload | None:
    if session is None:
        return None
    return {
        "scenario": str(session.scenario),
        "collected": dict(session.collected),
        "last_seen": session.last_seen,
    }


def session_from_payload(payload: ReplySessionPayload | None) -> ReplySession | None:
    if payload is None:
        return None
    return ReplySession(
        scenario=ReplyScenario(payload["scenario"]),
        collected=dict(payload["collected"]),
        last_seen=payload["last_seen"],
    )


def reply_to_payload(reply: RoutedReply) -> RoutedReplyPayload:
    return {
        "intent": str(reply.intent),
        "content": reply.content,
        "sources": reply.sources,
        "media_action": str(reply.media_action) if reply.media_action is not None else None,
        "scenario": str(reply.scenario) if reply.scenario is not None else None,
        "missing_fields": reply.missing_fields,
        "risk_warnings": reply.risk_warnings,
        "intent_confidence": reply.intent_confidence,
        "intent_reason": reply.intent_reason,
        "understood_query": reply.understood_query,
        "intent_source": reply.intent_source,
        "execution_trace": reply.execution_trace,
    }


def _optional_enum(enum_type: type[Any], value: str | None) -> Any:
    return enum_type(value) if value is not None else None


def reply_from_payload(payload: RoutedReplyPayload) -> RoutedReply:
    return RoutedReply(
        intent=Intent(payload["intent"]),
        content=payload["content"],
        sources=payload["sources"],
        media_action=_optional_enum(MediaAction, payload["media_action"]),
        scenario=_optional_enum(ReplyScenario, payload["scenario"]),
        missing_fields=payload["missing_fields"],
        risk_warnings=payload["risk_warnings"],
        intent_confidence=payload["intent_confidence"],
        intent_reason=payload["intent_reason"],
        understood_query=payload["understood_query"],
        intent_source=payload["intent_source"],
        execution_trace=payload.get("execution_trace", {}),
    )
