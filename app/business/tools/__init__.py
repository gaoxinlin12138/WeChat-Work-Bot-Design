"""Internal domain tools used by the business orchestrator.

These are deliberately internal facades, not model-facing tools and not API
endpoints.  Keeping the boundaries here makes it possible to reorganize the
implementation without changing the WeCom or admin contracts.
"""

from dataclasses import dataclass
from typing import Any

from app.business.tools.audit import AuditTool
from app.business.tools.knowledge import KnowledgeTool
from app.business.tools.media import MediaTool
from app.business.tools.product import ProductTool


@dataclass(frozen=True)
class BusinessTools:
    """Stable internal grouping for the business agent's domain boundaries."""

    knowledge: KnowledgeTool
    product: ProductTool
    media: MediaTool
    audit: AuditTool

    @classmethod
    def for_owner(cls, owner: Any) -> "BusinessTools":
        return cls(
            knowledge=KnowledgeTool(owner),
            product=ProductTool(owner),
            media=MediaTool(owner),
            audit=AuditTool(owner),
        )


__all__ = [
    "AuditTool",
    "BusinessTools",
    "KnowledgeTool",
    "MediaTool",
    "ProductTool",
]
