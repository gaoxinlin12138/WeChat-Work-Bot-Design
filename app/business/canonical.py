"""Auditable request boundary shared by routing, planning and execution.

The historical pipeline passed ``IntentDecision`` and ``ProductQuery`` through
several adapters.  That made it easy for one adapter to lose an ordinal,
pronoun or media request.  This module is deliberately small: it does not
query storage and it never invents a product.  It normalizes the already
validated plan and records what was resolved (or why it was not resolved).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.business.intent import (
    Intent,
    IntentDecision,
    MediaAction,
    ReplyScenario,
    is_product_catalog_query,
    media_requests,
)
from app.business.product_focus import ProductFocus, ordinal_index
from app.business.product_query import (
    ProductQuery,
    ProductReference,
    is_overview_phrase,
    is_overview_request,
)
from app.business.product_selection import product_identifier_tokens
from app.business.reference_terms import REFERENCE_PRONOUNS

CANONICAL_REQUEST_VERSION = 1
_PRONOUNS = REFERENCE_PRONOUNS


@dataclass(frozen=True)
class ReferenceResolution:
    """Deterministic reference resolution result used in the audit trail."""

    mode: str = "independent"
    references: tuple[ProductReference, ...] = ()
    product_ids: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return not self.unresolved

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "references": [
                {
                    "value": item.value,
                    "kind": item.kind,
                    "evidence": item.evidence,
                    "source": item.source,
                }
                for item in self.references
            ],
            "product_ids": list(self.product_ids),
            "models": list(self.models),
            "unresolved": list(self.unresolved),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class CanonicalTask:
    """One executable, auditable task at the request boundary."""

    intent: Intent
    query: str
    decision: IntentDecision
    product_query: ProductQuery | None = None
    media: tuple[tuple[str, str | None], ...] = ()
    resolution: ReferenceResolution = ReferenceResolution()
    needs_clarification: bool = False
    clarification_question: str = ""
    audit: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_intent_decision(
        cls,
        decision: IntentDecision,
        query: str,
        product_query: ProductQuery | None = None,
        *,
        focus: ProductFocus | None = None,
    ) -> CanonicalTask:
        """Adapt the legacy decision/product pair into one canonical task."""
        from app.business.planner import PlannedTask

        return canonicalize_task(
            PlannedTask(decision, product_query),
            query,
            focus=focus,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": str(self.intent),
            "query": self.query,
            "decision": {
                "intent": str(self.decision.intent),
                "confidence": self.decision.confidence,
                "reason": self.decision.reason,
                "query_for_search": self.decision.query_for_search,
                "source": self.decision.source,
                "needs_clarification": self.decision.needs_clarification,
                "clarification_question": self.decision.clarification_question,
                "media_action": str(self.decision.media_action)
                if self.decision.media_action is not None
                else None,
                "media_type": self.decision.media_type,
                "media_role": self.decision.media_role,
                "reply_scenario": str(self.decision.reply_scenario)
                if self.decision.reply_scenario is not None
                else None,
            },
            "product_query": _product_query_dict(self.product_query),
            "media": [[kind, role] for kind, role in self.media],
            "resolution": self.resolution.to_dict(),
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "audit": dict(self.audit),
        }


@dataclass(frozen=True)
class CanonicalRequest:
    """Versioned request envelope consumed by the graph and audit log."""

    query: str
    tasks: tuple[CanonicalTask, ...]
    request_id: str | None = None
    source: str = "canonical"
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: str = ""
    unresolved: tuple[str, ...] = ()
    version: int = CANONICAL_REQUEST_VERSION

    @classmethod
    def from_task_plan(
        cls,
        query: str,
        plan: Any,
        *,
        focus: ProductFocus | None = None,
        request_id: str | None = None,
    ) -> CanonicalRequest:
        return canonicalize_request(query, plan, focus=focus, request_id=request_id)

    @classmethod
    def from_intent_decision(
        cls,
        query: str,
        decision: IntentDecision,
        product_query: ProductQuery | None = None,
        *,
        focus: ProductFocus | None = None,
        request_id: str | None = None,
    ) -> CanonicalRequest:
        from app.business.planner import PlannedTask

        return canonicalize_request(
            query,
            (PlannedTask(decision, product_query),),
            focus=focus,
            request_id=request_id,
        )

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            self.to_dict(include_fingerprint=False), ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload = {
            "version": self.version,
            "request_id": self.request_id,
            "query": self.query,
            "source": self.source,
            "confidence": self.confidence,
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "unresolved": list(self.unresolved),
            "tasks": [task.to_dict() for task in self.tasks],
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    def json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CanonicalRequest:
        """Restore an audit envelope without invoking a model or a database."""
        if not isinstance(payload, Mapping):
            raise ValueError("CanonicalRequest 必须是对象")
        if int(payload.get("version", CANONICAL_REQUEST_VERSION)) != CANONICAL_REQUEST_VERSION:
            raise ValueError("CanonicalRequest 版本不受支持")
        restored: list[CanonicalTask] = []
        for raw in payload.get("tasks", ()) or ():
            if not isinstance(raw, Mapping):
                raise ValueError("CanonicalTask 必须是对象")
            raw_decision = raw.get("decision")
            if not isinstance(raw_decision, Mapping):
                raise ValueError("CanonicalTask 缺少 decision")
            intent = Intent(str(raw_decision.get("intent") or raw.get("intent")))
            media_action = raw_decision.get("media_action")
            reply_scenario = raw_decision.get("reply_scenario")
            decision = IntentDecision(
                intent=intent,
                confidence=float(raw_decision.get("confidence") or 0.0),
                reason=str(raw_decision.get("reason") or ""),
                query_for_search=str(
                    raw_decision.get("query_for_search") or raw.get("query") or ""
                ),
                source=str(raw_decision.get("source") or "canonical"),
                needs_clarification=raw_decision.get("needs_clarification") is True,
                clarification_question=str(raw_decision.get("clarification_question") or ""),
                media_action=MediaAction(str(media_action)) if media_action else None,
                media_type=str(raw_decision.get("media_type"))
                if raw_decision.get("media_type")
                else None,
                media_role=str(raw_decision.get("media_role"))
                if raw_decision.get("media_role")
                else None,
                reply_scenario=ReplyScenario(str(reply_scenario)) if reply_scenario else None,
            )
            raw_query = raw.get("product_query")
            product_query = None
            if isinstance(raw_query, Mapping):
                query_references = tuple(
                    ProductReference(
                        str(item.get("value") or ""),
                        str(item.get("kind") or "fragment"),  # type: ignore[arg-type]
                        str(item.get("evidence") or ""),
                        str(item.get("source") or "current"),  # type: ignore[arg-type]
                    )
                    for item in raw_query.get("references", ()) or ()
                    if isinstance(item, Mapping) and item.get("value")
                )
                product_query = ProductQuery(
                    operation=str(raw_query.get("operation") or "detail"),  # type: ignore[arg-type]
                    target=str(raw_query.get("target") or "product"),  # type: ignore[arg-type]
                    version=int(raw_query.get("version") or 2),
                    include_models=tuple(
                        str(item) for item in raw_query.get("include_models", ()) or ()
                    ),
                    fields=tuple(str(item) for item in raw_query.get("fields", ()) or ()),
                    references=query_references,
                    allow_overview=raw_query.get("allow_overview") is True,
                    context_mode=str(raw_query.get("context_mode") or "independent"),  # type: ignore[arg-type]
                    resolved_product_ids=tuple(
                        str(item) for item in raw_query.get("resolved_product_ids", ()) or ()
                    ),
                )
            raw_resolution = raw.get("resolution")
            raw_resolution = raw_resolution if isinstance(raw_resolution, Mapping) else {}
            models = tuple(str(item) for item in raw_resolution.get("models", ()) or ())
            raw_references = raw_resolution.get("references", ())
            resolution_references = tuple(
                ProductReference(
                    str(item.get("value") or ""),
                    str(item.get("kind") or "fragment"),  # type: ignore[arg-type]
                    str(item.get("evidence") or ""),
                    str(item.get("source") or "current"),  # type: ignore[arg-type]
                )
                for item in raw_references
                if isinstance(item, Mapping) and item.get("value")
            )
            resolution = ReferenceResolution(
                mode=str(raw_resolution.get("mode") or "independent"),
                references=resolution_references
                or tuple(ProductReference(item, "context", item, "context") for item in models),
                product_ids=tuple(
                    str(item) for item in raw_resolution.get("product_ids", ()) or ()
                ),
                models=models,
                unresolved=tuple(str(item) for item in raw_resolution.get("unresolved", ()) or ()),
                evidence=tuple(str(item) for item in raw_resolution.get("evidence", ()) or ()),
            )
            media = tuple(
                (str(item[0]), str(item[1]) if len(item) > 1 and item[1] is not None else None)
                for item in raw.get("media", ()) or ()
                if isinstance(item, (list, tuple)) and item
            )
            restored.append(
                CanonicalTask(
                    intent=intent,
                    query=str(raw.get("query") or ""),
                    decision=decision,
                    product_query=product_query,
                    media=media,
                    resolution=resolution,
                    needs_clarification=raw.get("needs_clarification") is True,
                    clarification_question=str(raw.get("clarification_question") or ""),
                    audit=dict(raw.get("audit") or {})
                    if isinstance(raw.get("audit"), Mapping)
                    else {},
                )
            )
        return cls(
            query=str(payload.get("query") or ""),
            tasks=tuple(restored),
            request_id=str(payload.get("request_id")) if payload.get("request_id") else None,
            source=str(payload.get("source") or "canonical"),
            confidence=float(payload.get("confidence") or 0.0),
            needs_clarification=payload.get("needs_clarification") is True,
            clarification_question=str(payload.get("clarification_question") or ""),
            unresolved=tuple(str(item) for item in payload.get("unresolved", ()) or ()),
        )

    @classmethod
    def from_json(cls, value: str) -> CanonicalRequest:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("CanonicalRequest JSON 无效") from exc
        return cls.from_dict(payload)

    def to_task_plan(self):
        """Compatibility bridge for the existing graph executor."""
        from app.business.planner import PlannedTask, TaskPlan

        return TaskPlan(
            goal=self.query[:500],
            tasks=tuple(PlannedTask(task.decision, task.product_query) for task in self.tasks),
            confidence=self.confidence,
            reason="统一请求中间层已规范化",
            source=self.source,
            needs_clarification=self.needs_clarification,
            clarification_question=self.clarification_question,
        )


def _product_query_dict(query: ProductQuery | None) -> dict[str, object] | None:
    if query is None:
        return None
    # Keep this projection intentionally stable and JSON-only.  The graph's
    # state serializer remains the authoritative full ProductQuery roundtrip.
    return {
        "version": query.version,
        "operation": query.operation,
        "target": query.target,
        "include_models": list(query.include_models),
        "fields": list(query.fields),
        "allow_overview": query.allow_overview,
        "context_mode": query.context_mode,
        "resolved_product_ids": list(query.resolved_product_ids),
        "references": [
            {
                "value": item.value,
                "kind": item.kind,
                "evidence": item.evidence,
                "source": item.source,
            }
            for item in query.effective_references()
        ],
    }


def _focus_resolution(query: str, focus: ProductFocus | None) -> ReferenceResolution:
    explicit = tuple(product_identifier_tokens(query))
    references = tuple(
        ProductReference(
            value, "exact" if re.search(r"[A-Za-z]", value) else "fragment", value, "current"
        )
        for value in explicit
    )
    ordinal = ordinal_index(query)
    if ordinal is not None:
        marker = ordinal[1]
        if focus is None or not focus.is_valid() or focus.item_at(ordinal[0]) is None:
            return ReferenceResolution(
                mode="continuation", references=references, unresolved=(marker,), evidence=(marker,)
            )
        product_id, model = focus.item_at(ordinal[0])  # type: ignore[misc]
        return ReferenceResolution(
            mode="continuation",
            references=(ProductReference(model, "context", marker, "context"),),
            product_ids=(product_id,),
            models=(model,),
            evidence=(marker,),
        )

    pronoun = next((word for word in _PRONOUNS if word in query), None)
    if pronoun:
        if (
            focus is not None
            and focus.is_valid()
            and (
                len(focus.result_ids) == 1
                or (not focus.result_ids and len(focus.result_models) == 1)
            )
        ):
            product_id, model = focus.item_at(0)  # type: ignore[misc]
            return ReferenceResolution(
                mode="continuation",
                references=(ProductReference(model, "context", pronoun, "context"),),
                product_ids=(product_id,),
                models=(model,),
                evidence=(pronoun,),
            )
        return ReferenceResolution(mode="continuation", unresolved=(pronoun,), evidence=(pronoun,))

    return ReferenceResolution(
        mode="independent", references=references, models=explicit, evidence=explicit
    )


def canonicalize_task(
    task: Any,
    query: str,
    *,
    focus: ProductFocus | None = None,
    preserve_media_split: bool = False,
) -> CanonicalTask:
    """Normalize one ``PlannedTask`` without making an unresolved guess."""
    decision: IntentDecision = task.decision
    product_query: ProductQuery | None = task.product_query
    resolution = _focus_resolution(query, focus)
    if product_query is not None:
        if resolution.unresolved:
            # Preserve the model proposal but prevent execution from treating
            # an unresolved pronoun as an independently identified product.
            product_query = replace(
                product_query, include_models=(), references=(), resolved_product_ids=()
            )
        elif resolution.product_ids or resolution.models:
            model_values: list[str] = []
            seen_models: set[str] = set()
            for model in (*product_query.include_models, *resolution.models):
                key = model.casefold()
                if key in seen_models:
                    continue
                seen_models.add(key)
                model_values.append(model)
            models = tuple(model_values)
            refs = product_query.references or resolution.references
            product_query = replace(
                product_query,
                include_models=models,
                references=refs,
                resolved_product_ids=resolution.product_ids or product_query.resolved_product_ids,
                context_mode=(
                    "continuation"
                    if resolution.mode == "continuation"
                    else product_query.context_mode
                ),
            )
        refs = product_query.effective_references()
        if (
            product_query.operation == "list"
            and not refs
            and not product_query.filters
            and not product_query.effective_field_requests()
            and is_product_catalog_query(query)
        ):
            # An unscoped catalogue question ("现在有什么产品") is itself a
            # valid overview scope; it must not be mistaken for a missing
            # colour/material/model constraint.
            product_query = replace(product_query, allow_overview=True)
        if product_query.operation == "detail" and (
            is_overview_phrase(query)
            or is_overview_request(query, refs)
            or bool(re.search(r"详细信息|完整资料|全部参数|所有参数|完整信息|产品详情", query))
        ):
            product_query = replace(
                product_query,
                fields=(),
                requested_fields=(),
                original_fields=(),
                allow_overview=True,
            )

    media = media_requests(query) if decision.intent == Intent.PRODUCT_MEDIA else ()
    # The graph's legacy planner may already have fanned out a compound
    # request.  Do not fan each pre-split task out again.
    if preserve_media_split and decision.media_type and media:
        matching = tuple(
            item
            for item in media
            if item[0] == decision.media_type
            and (decision.media_role is None or item[1] == decision.media_role)
        )
        if matching:
            media = matching[:1]
    if not media and decision.media_type:
        media = ((decision.media_type, decision.media_role),)
    unresolved = resolution.unresolved
    # A model may conservatively ask for a model even though the server has
    # already resolved the pronoun against one trusted focus item.  The trusted
    # context wins; only genuinely unresolved references keep clarification on.
    resolved_context = resolution.mode == "continuation" and bool(
        resolution.product_ids or resolution.models
    )
    needs = bool(unresolved or (decision.needs_clarification and not resolved_context))
    question = "" if resolved_context else decision.clarification_question
    if unresolved and not question:
        question = "请明确要查询哪一个产品或序号，我不能替你猜测。"
    if unresolved and decision.intent == Intent.PRODUCT_MEDIA:
        # Media execution is product-scoped; an unresolved pronoun must not be
        # reinterpreted as a customer/person.  Keep the clarification useful
        # and deterministic while still refusing to guess the model.
        question = "请提供具体产品型号，我再为你查找对应媒体。"
    audit = {
        "raw_query": query[:1000],
        "resolution": resolution.to_dict(),
        "overview_semantics": bool(product_query and product_query.allow_overview),
        "media_requests": [[kind, role] for kind, role in media],
        "unresolved": list(unresolved),
    }
    return CanonicalTask(
        intent=decision.intent,
        query=query,
        decision=replace(decision, needs_clarification=needs, clarification_question=question),
        product_query=product_query,
        media=media,
        resolution=resolution,
        needs_clarification=needs,
        clarification_question=question,
        audit=audit,
    )


def canonicalize_request(
    query: str,
    plan: Any,
    *,
    focus: ProductFocus | None = None,
    request_id: str | None = None,
) -> CanonicalRequest:
    """Build the single request envelope from a ``TaskPlan`` or task iterable."""
    raw_tasks = getattr(plan, "tasks", plan)
    raw_tasks = tuple(raw_tasks)
    tasks = tuple(
        canonicalize_task(item, query, focus=focus, preserve_media_split=len(raw_tasks) > 1)
        for item in raw_tasks
    )
    # A compound image+video request is represented as two canonical tasks,
    # while preserving all other task ordering and provenance.
    expanded: list[CanonicalTask] = []
    for task in tasks:
        if len(task.media) <= 1:
            expanded.append(task)
            continue
        for media_type, media_role in task.media:
            decision = replace(task.decision, media_type=media_type, media_role=media_role)
            expanded.append(replace(task, decision=decision, media=((media_type, media_role),)))
    tasks = tuple(expanded)
    unresolved = tuple(dict.fromkeys(item for task in tasks for item in task.resolution.unresolved))
    needs = bool(
        getattr(plan, "needs_clarification", False)
        or any(task.needs_clarification for task in tasks)
    )
    question = next(
        (task.clarification_question for task in tasks if task.clarification_question), ""
    )
    return CanonicalRequest(
        query=query,
        tasks=tasks,
        request_id=request_id,
        source=str(getattr(plan, "source", "canonical")),
        confidence=float(getattr(plan, "confidence", 0.0) or 0.0),
        needs_clarification=needs,
        clarification_question=question,
        unresolved=unresolved,
    )


build_canonical_request = canonicalize_request
canonical_request_from_plan = canonicalize_request
