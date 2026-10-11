"""Permission-safe summaries for product query outcomes and audit records."""

import hashlib
import re

from app.business.product_query import PRODUCT_FIELD_CATALOG
from app.business.tools.common import RoutedReply


def canonical_product_content(content: str) -> str:
    # Preserve order: sorting lines could attach product A's facts to product B.
    return "\n".join(
        re.sub(r"\s+", "", line).replace(":", "：") for line in content.splitlines() if line.strip()
    )


def product_outcome(
    reply: RoutedReply,
    *,
    requested_fields: tuple[str, ...] = (),
    result_type: str | None = None,
    reason: str = "",
) -> dict[str, object]:
    # Legacy replies have no typed result; classify their actual rendered answer,
    # never the (potentially much larger) source document or proposed V2 plan.
    content = reply.content
    field_labels = "|".join(re.escape(item.label) for item in PRODUCT_FIELD_CATALOG.values())
    has_fact_lines = bool(re.search(rf"(?:^|[\n｜|])\s*(?:{field_labels})[:：]", content))
    denied = any(word in content for word in ("没有查看", "无权限", "没有权限"))
    if result_type is None:
        if denied and not has_fact_lines:
            result_type = "permission_denied"
        elif reply.sources:
            result_type = "fact"
        elif any(word in content for word in ("暂时不可用", "稍后重试", "暂时无法完成")):
            result_type = "error"
        elif any(word in content for word in ("无法安全", "无法可靠")):
            result_type = "validation_failed"
        elif any(
            word in content
            for word in (
                "没有符合",
                "未找到",
                "没有找到",
                "没有查到",
                "没有已发布",
                "已停用",
            )
        ):
            result_type = "no_result"
        elif any(word in content for word in ("请", "还是", "哪一", "哪个")):
            result_type = "clarification"
        else:
            result_type = "unknown"
    answered = (
        sorted(
            name
            for name, definition in PRODUCT_FIELD_CATALOG.items()
            if re.search(
                rf"(?:^|[\n｜|])\s*(?:\d+[.、]\s*)?{re.escape(definition.label)}(?:明细)?[:：（(]",
                content,
            )
        )
        if result_type == "fact"
        else []
    )
    # Never copy source records, cost values, or component purchase prices to diff.
    safe = re.sub(
        r"(?:成本(?:价)?|采购(?:价|总价|小计|总额)|进价)\s*[:：][^\n｜|]*",
        "敏感价格：[已脱敏]",
        content,
    )
    lines = [line.strip() for line in safe.splitlines() if line.strip()]
    canonical = canonical_product_content(safe)
    return {
        "result_type": result_type,
        "reason": reason,
        "requested_fields": sorted(set(requested_fields)),
        "answered_fields": answered,
        "content_summary": "\n".join(lines)[:800],
        "content_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "permission_denied": denied or result_type == "permission_denied",
    }
