import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

_TERM_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")


@dataclass(frozen=True)
class HybridMatch:
    key: str
    score: float
    semantic_score: float
    bm25_score: float


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return " ".join(normalized.split())


def tokenize(value: str) -> list[str]:
    """为中文知识库生成稳定词项：英文/编号按词，中文采用二元词项。"""
    terms: list[str] = []
    for group in _TERM_PATTERN.findall(normalize_text(value)):
        if group.isascii():
            terms.append(group)
            continue
        if len(group) == 1:
            terms.append(group)
            continue
        terms.extend(group[index : index + 2] for index in range(len(group) - 1))
        if len(group) <= 8:
            terms.append(group)
    return terms


def bm25_scores(
    query: str,
    documents: Mapping[str, str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> dict[str, float]:
    """计算归一化 BM25 分数，返回值范围为 0 到 1。"""
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not documents:
        return {}
    tokenized = {key: tokenize(text) for key, text in documents.items()}
    document_count = len(tokenized)
    average_length = sum(len(terms) for terms in tokenized.values()) / document_count
    if average_length <= 0:
        return {}
    document_frequency = {
        term: sum(term in terms for terms in tokenized.values()) for term in query_terms
    }
    raw_scores: dict[str, float] = {}
    for key, terms in tokenized.items():
        frequencies = Counter(terms)
        length_ratio = len(terms) / average_length
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            frequency_count = document_frequency[term]
            inverse_frequency = math.log(
                1 + (document_count - frequency_count + 0.5) / (frequency_count + 0.5)
            )
            denominator = frequency + k1 * (1 - b + b * length_ratio)
            score += inverse_frequency * (frequency * (k1 + 1) / denominator)
        if score > 0:
            raw_scores[key] = score
    maximum = max(raw_scores.values(), default=0.0)
    return {key: score / maximum for key, score in raw_scores.items()} if maximum else {}


def hybrid_rerank(
    query: str,
    documents: Mapping[str, str],
    semantic_scores: Mapping[str, float],
    *,
    limit: int,
    candidate_limit: int = 20,
    quality_scores: Mapping[str, float] | None = None,
) -> list[HybridMatch]:
    """双路召回，经 RRF 合并后使用跨信号分数做第二阶段重排。"""
    if limit <= 0 or not documents:
        return []
    quality_scores = quality_scores or {}
    lexical_scores = bm25_scores(query, documents)
    semantic_rank = [
        key
        for key, _ in sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True)[
            :candidate_limit
        ]
        if key in documents
    ]
    lexical_rank = [
        key
        for key, _ in sorted(lexical_scores.items(), key=lambda item: item[1], reverse=True)[
            :candidate_limit
        ]
    ]
    ranked_channels = [rank for rank in (semantic_rank, lexical_rank) if rank]
    if not ranked_channels:
        return []

    rrf_constant = 60
    rrf_scores: dict[str, float] = {}
    for rank in ranked_channels:
        for position, key in enumerate(rank, start=1):
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1 / (rrf_constant + position)
    maximum_rrf = len(ranked_channels) / (rrf_constant + 1)

    query_terms = set(tokenize(query))
    normalized_query = normalize_text(query).replace(" ", "")
    matches: list[HybridMatch] = []
    for key, rrf_score in rrf_scores.items():
        semantic = max(0.0, min(1.0, float(semantic_scores.get(key, 0.0))))
        lexical = lexical_scores.get(key, 0.0)
        document_terms = set(tokenize(documents[key]))
        coverage = len(query_terms & document_terms) / len(query_terms) if query_terms else 0.0
        normalized_document = normalize_text(documents[key]).replace(" ", "")
        exact = float(bool(normalized_query and normalized_query in normalized_document))
        quality = max(0.0, min(1.0, float(quality_scores.get(key, 0.0))))
        score = (
            0.35 * (rrf_score / maximum_rrf)
            + 0.25 * semantic
            + 0.20 * lexical
            + 0.10 * coverage
            + 0.05 * exact
            + 0.05 * quality
        )
        matches.append(HybridMatch(key, min(1.0, score), semantic, lexical))
    return sorted(matches, key=lambda match: (-match.score, match.key))[:limit]
