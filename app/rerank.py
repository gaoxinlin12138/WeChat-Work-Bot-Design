"""候选结果的第二阶段重排。

第一阶段仍由 BM25 + 向量在本地完成；本模块只把少量候选文本发送给
百炼重排。任何配置、网络或响应格式问题都会返回空结果，由调用方保留
本地排序，避免云服务故障阻断企业微信问答。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings
from app.retrieval import HybridMatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankScore:
    key: str
    score: float


def _as_score(value: object) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    return max(0.0, min(1.0, score))


def _parse_results(payload: Mapping[str, object], keys: Sequence[str]) -> list[RerankScore]:
    output = payload.get("output")
    if not isinstance(output, Mapping):
        return []
    raw_results = output.get("results")
    if not isinstance(raw_results, list):
        return []
    parsed: list[RerankScore] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        index = item.get("index")
        try:
            key = keys[int(index)]
        except (TypeError, ValueError, IndexError):
            continue
        score = _as_score(item.get("relevance_score", item.get("score")))
        if score is not None:
            parsed.append(RerankScore(key, score))
    return parsed


def dashscope_rerank(query: str, documents: Sequence[tuple[str, str]]) -> list[RerankScore]:
    """调用百炼 Text Rerank API；失败时返回空列表。"""
    settings = get_settings()
    if (
        not settings.rerank_enabled
        or settings.rerank_provider != "dashscope"
        or not settings.rerank_api_key
        or not settings.rerank_model
        or not query.strip()
        or not documents
    ):
        return []
    keys = [key for key, _ in documents]
    texts = [text[:6000] for _, text in documents]
    body = {
        "model": settings.rerank_model,
        "input": {"query": query[:2000], "documents": texts},
        "parameters": {"return_documents": False, "top_n": len(texts)},
    }
    request = Request(
        settings.rerank_base_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.rerank_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.rerank_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            return []
        return _parse_results(payload, keys)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("百炼重排调用失败，继续使用本地排序: %s", exc)
        return []


def rerank_matches(
    query: str,
    documents: Mapping[str, str],
    matches: Sequence[HybridMatch],
    *,
    limit: int,
) -> list[HybridMatch]:
    """对本地候选进行云端重排；没有云端结果时保留本地顺序。"""
    if limit <= 0:
        return []
    local = list(matches)
    if not local:
        return []
    settings = get_settings()
    candidate_limit = max(limit, settings.rerank_candidate_limit)
    candidates = [match for match in local[:candidate_limit] if match.key in documents]
    if not candidates:
        return []
    scores = dashscope_rerank(query, [(match.key, documents[match.key]) for match in candidates])
    if not scores:
        return candidates[:limit]
    score_map = {item.key: item.score for item in scores}
    ranked = sorted(
        candidates,
        key=lambda match: (
            -score_map.get(match.key, -1.0),
            -match.score,
            match.key,
        ),
    )
    return ranked[:limit]
