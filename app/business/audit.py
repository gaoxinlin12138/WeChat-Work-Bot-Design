import hashlib


def normalize_audit_text(value: str | None, limit: int = 500) -> str:
    """保留管理员可核对的完整原话，仅规范空白并限制字段长度。"""
    return " ".join(str(value or "").split())[:limit]


def text_fingerprint(value: str | None) -> str:
    """只用于去重和聚合的不可逆指纹，不可还原原文。"""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
