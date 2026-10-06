import os
import re
from functools import lru_cache

from app.config import get_settings
from app.knowledge.store import KnowledgeStore, SearchResult

_CLAUSE_PATTERN = re.compile(r"(?<![\d.])([1-9]\d?(?:\.\d+){1,3})(?![\d.])")


def extract_clause_references(content: str, limit: int = 8) -> list[str]:
    references: list[str] = []
    for reference in _CLAUSE_PATTERN.findall(content):
        if reference not in references:
            references.append(reference)
        if len(references) >= limit:
            break
    return references


def format_source_citation(result: SearchResult) -> str:
    parts = [f"《{result.title}》"]
    clauses = extract_clause_references(result.content, limit=4)
    if clauses:
        suffix = "等" if len(extract_clause_references(result.content, limit=5)) > 4 else ""
        parts.append(f"第{'、'.join(clauses)}{suffix}条")
    if result.page_number:
        parts.append(f"PDF第{result.page_number}页")
    return f"【{'｜'.join(parts)}】"


def format_source_citations(results: list[SearchResult]) -> list[str]:
    """将同一资料、同一页的多个检索片段合并为一条可核对引用。"""
    grouped: dict[tuple[str, str, int | None], list[str]] = {}
    for result in results:
        key = (result.document_id, result.title, result.page_number)
        clauses = grouped.setdefault(key, [])
        for clause in extract_clause_references(result.content):
            if clause not in clauses:
                clauses.append(clause)
    citations: list[str] = []
    for (_, title, page_number), clauses in grouped.items():
        parts = [f"《{title}》"]
        if clauses:
            parts.append(f"第{'、'.join(clauses)}条")
        if page_number:
            parts.append(f"PDF第{page_number}页")
        citations.append(f"【{'｜'.join(parts)}】")
    return citations


@lru_cache
def get_knowledge_store() -> KnowledgeStore:
    settings = get_settings()
    if settings.app_env == "test" and os.environ.get("CODEX_TEST_ALLOW_APP_DATABASE") != "1":
        raise RuntimeError("测试环境禁止通过应用工厂连接数据库；请使用 isolated_database_url")
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL 尚未配置")
    store = KnowledgeStore(
        settings.database_url,
        settings.knowledge_upload_dir,
        settings.embedding_dimensions,
    )
    store.initialize()
    return store


def format_search_reply(query: str, results: list[SearchResult]) -> str:
    if not results:
        return f"知识库中暂未找到与「{query}」直接匹配的已发布资料。"
    sections = ["在已发布资料中找到以下内容："]
    for result in results[:3]:
        excerpt = result.content[:260].strip()
        sections.append(f"\n{excerpt}")
    sections.append(
        "\n引用依据：\n"
        + "\n".join(f"- {citation}" for citation in format_source_citations(results[:3]))
    )
    return "\n".join(sections)
