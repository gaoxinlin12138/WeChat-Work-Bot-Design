"""Unified, short-lived product conversation focus.

The product domain historically kept three independent context maps.  This
module provides the single serializable envelope used by new code while the
callers retain a small compatibility reader for older payloads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

FOCUS_VERSION = 1
FOCUS_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class ProductFocus:
    version: int = FOCUS_VERSION
    mode: str = "list"
    query_plan: dict[str, object] | None = None
    result_ids: tuple[str, ...] = ()
    result_models: tuple[str, ...] = ()
    page: int = 1
    total: int = 0
    source_query_fingerprint: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    pending: dict[str, object] | None = None

    def is_valid(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.expires_at <= 0 or self.expires_at >= current

    def item_at(self, index: int) -> tuple[str, str] | None:
        if index < 0:
            return None
        if not self.result_ids and index < len(self.result_models):
            model = self.result_models[index]
            return model, model
        if index >= len(self.result_ids):
            return None
        product_id = self.result_ids[index]
        model = self.result_models[index] if index < len(self.result_models) else ""
        return product_id, model

    def model_to_id(self, model: str) -> str | None:
        needle = model.strip().casefold()
        for product_id, shown_model in zip(self.result_ids, self.result_models, strict=False):
            if shown_model.strip().casefold() == needle:
                return product_id
        return None


def focus_to_dict(focus: ProductFocus) -> dict[str, object]:
    return {
        "version": focus.version,
        "mode": focus.mode,
        "query_plan": focus.query_plan or {},
        "result_ids": list(focus.result_ids),
        "result_models": list(focus.result_models),
        "page": focus.page,
        "total": focus.total,
        "source_query_fingerprint": focus.source_query_fingerprint,
        "created_at": focus.created_at,
        "expires_at": focus.expires_at,
        "pending": focus.pending,
    }


def focus_from_dict(value: object) -> ProductFocus | None:
    if not isinstance(value, dict):
        return None
    try:
        raw_ids = value.get("result_ids", [])
        raw_models = value.get("result_models", [])
        ids = (
            tuple(str(item) for item in raw_ids if str(item).strip())
            if isinstance(raw_ids, (list, tuple))
            else ()
        )
        models = (
            tuple(str(item) for item in raw_models)
            if isinstance(raw_models, (list, tuple))
            else ()
        )
        raw_plan = value.get("query_plan")
        return ProductFocus(
            version=int(value.get("version") or FOCUS_VERSION),
            mode=str(value.get("mode") or "list"),
            query_plan=dict(raw_plan) if isinstance(raw_plan, dict) and raw_plan else None,
            result_ids=ids,
            result_models=models,
            page=max(1, int(value.get("page") or 1)),
            total=max(0, int(value.get("total") or 0)),
            source_query_fingerprint=str(value.get("source_query_fingerprint") or ""),
            created_at=float(value.get("created_at") or 0.0),
            expires_at=float(value.get("expires_at") or 0.0),
            pending=(dict(value["pending"]) if isinstance(value.get("pending"), dict) else None),
        )
    except (TypeError, ValueError):
        return None


def legacy_focus_payload(
    *,
    plan: dict[str, object] | None,
    result_ids: list[str] | tuple[str, ...] = (),
    result_models: list[str] | tuple[str, ...] = (),
    page: int = 1,
    total: int = 0,
    mode: str = "list",
    pending: dict[str, object] | None = None,
) -> dict[str, object]:
    now = time.time()
    return focus_to_dict(
        ProductFocus(
            mode=mode,
            query_plan=plan,
            result_ids=tuple(str(item) for item in result_ids),
            result_models=tuple(str(item) for item in result_models),
            page=max(1, int(page)),
            total=max(0, int(total)),
            created_at=now,
            expires_at=now + FOCUS_TTL_SECONDS,
            pending=pending,
        )
    )


def focus_from_legacy(
    value: object, *, catalog: object = None, choices: object = None
) -> ProductFocus | None:
    """Best-effort one-time conversion of the previous state layouts."""
    if isinstance(value, dict):
        plan = value.get("plan")
        ids = value.get("selected_product_ids") or value.get("ids") or ()
        models = value.get("selected_product_models") or ()
        # The old validated-query state wins only when it contains the actual
        # displayed IDs/models.  A plan-only query state must not mask the old
        # catalog state, which may be the only state carrying display order.
        if ids or models:
            raw_mode = str(value.get("mode") or "list")
            mode = {
                "v2": "list",
                "v2_aggregate": "aggregate",
                "v2_detail": "detail",
            }.get(raw_mode, raw_mode)
            payload = legacy_focus_payload(
                plan=plan if isinstance(plan, dict) else None,
                result_ids=ids if isinstance(ids, (list, tuple)) else (),
                result_models=models if isinstance(models, (list, tuple)) else (),
                page=int(value.get("page") or 1),
                total=int(value.get("total") or 0),
                mode=mode,
            )
            if value.get("expires_at") is not None:
                payload["expires_at"] = value.get("expires_at")
            return focus_from_dict(payload)
    if isinstance(catalog, dict):
        plan = catalog.get("query") or catalog.get("plan")
        ids = catalog.get("ids") or catalog.get("result_ids") or ()
        models = catalog.get("models") or catalog.get("result_models") or ()
        if isinstance(plan, dict) or ids:
            raw_mode = str(catalog.get("mode") or "list")
            mode = {
                "v2": "list",
                "v2_aggregate": "aggregate",
                "v2_detail": "detail",
                "v2_family_detail": "detail",
                "family_detail": "detail",
                "v2_family_summary": "list",
                "family_summary": "list",
            }.get(raw_mode, raw_mode)
            payload = legacy_focus_payload(
                plan=plan if isinstance(plan, dict) else None,
                result_ids=ids if isinstance(ids, (list, tuple)) else (),
                result_models=models if isinstance(models, (list, tuple)) else (),
                page=int(catalog.get("page") or 1),
                total=int(catalog.get("total") or catalog.get("total_values") or 0),
                mode=mode,
            )
            if catalog.get("expires_at") is not None:
                payload["expires_at"] = catalog.get("expires_at")
            return focus_from_dict(payload)
    if isinstance(value, dict) and isinstance(value.get("plan"), dict):
        # Preserve a plan-only legacy count/continuation after checking the
        # catalog and choices layouts above.
        payload = legacy_focus_payload(
            plan=value["plan"],
            page=int(value.get("page") or 1),
            total=int(value.get("total") or 0),
            mode={
                "v2": "list",
                "v2_aggregate": "aggregate",
                "v2_detail": "detail",
            }.get(str(value.get("mode") or "list"), str(value.get("mode") or "list")),
        )
        if value.get("expires_at") is not None:
            payload["expires_at"] = value.get("expires_at")
        return focus_from_dict(payload)
    if isinstance(choices, dict):
        ids = choices.get("ids") or ()
        if isinstance(ids, (list, tuple)):
            return focus_from_dict(
                legacy_focus_payload(
                    plan=None,
                    result_ids=ids,
                    page=1,
                    total=len(ids),
                    mode="selection",
                    pending=choices,
                )
            )
    return None


def ordinal_index(query: str) -> tuple[int, str] | None:
    """Return zero-based ordinal index and the matched marker."""
    import re

    text = query.strip()
    markers = (
        ("后者", 1),
        ("前者", 0),
    )
    for marker, index in markers:
        if marker in text:
            return index, marker
    match = re.search(r"第\s*([一二两三四五六七八九十百\d]+)\s*(?:个|款|种|项|产品)", text)
    if not match:
        match = re.search(r"(?:第)?([一二两三四五六七八九\d]+)\s*(?:个|款|种|项)(?!页)", text)
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        number = int(value)
    else:
        numbers = dict(zip("一二两三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
        if value == "十":
            number = 10
        elif "十" in value:
            tens, units = value.split("十", 1)
            if tens and tens not in numbers or units and units not in numbers:
                return None
            number = numbers.get(tens, 1) * 10 + numbers.get(units, 0)
        else:
            number = numbers.get(value, 0)
    return (number - 1, match.group(0)) if number > 0 else None

