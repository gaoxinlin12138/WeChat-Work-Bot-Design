from typing import Any


class AuditTool:
    """Boundary for intent/result audit persistence."""

    name = "audit"

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def record_audit(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner._record_intent_audit(*args, **kwargs)
