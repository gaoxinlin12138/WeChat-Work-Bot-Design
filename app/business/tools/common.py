"""Shared value objects for the internal business tool boundaries."""

import time
from dataclasses import dataclass, field

from app.business.intent import Intent, MediaAction, ReplyScenario


@dataclass(frozen=True)
class RoutedReply:
    """The stable result passed from a domain tool to the orchestrator."""

    intent: Intent
    content: str
    sources: list[dict[str, object]]
    media_action: MediaAction | None = None
    scenario: ReplyScenario | None = None
    missing_fields: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    intent_confidence: float = 0.0
    intent_reason: str = ""
    understood_query: str = ""
    intent_source: str = ""
    execution_trace: dict[str, object] = field(default_factory=dict)


@dataclass
class ReplySession:
    """短期话术会话状态；进程内或 Redis 保存，不写入业务表。"""

    scenario: ReplyScenario
    collected: dict[str, str] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)
