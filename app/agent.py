import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import structlog
from openai import OpenAI

from app.business.response_style import KNOWLEDGE_RESPONSE_GUIDELINES
from app.config import Settings, get_settings
from app.knowledge.service import (
    extract_clause_references,
    format_search_reply,
    format_source_citations,
    get_knowledge_store,
)
from app.knowledge.store import ChatMessage, KnowledgeStore, SearchResult

logger = structlog.get_logger()


@dataclass(frozen=True)
class AgentReply:
    content: str
    sources: list[SearchResult]


_MODEL_CITATION_PATTERN = re.compile(r"\s*【《[^】]+》(?:｜[^】]+)*】")
_INTERNAL_MARKER_PATTERN = re.compile(
    r"\s*\[(?:资料|证据)\s*\d+\]|\s*\[E\d+(?::[0-9.、,，\s]+)?\]",
    re.IGNORECASE,
)
_EVIDENCE_SELECTION_PATTERN = re.compile(r"\[E(\d+)(?::([0-9.、,，\s]+))?\]", re.IGNORECASE)
_MODEL_REFERENCE_SECTION = re.compile(r"\n*\s*(?:引用依据|参考依据|资料出处)\s*[：:].*$", re.DOTALL)
_INCOMPLETE_INTERNAL_MARKER = re.compile(
    r"\s*\[(?:E\d*(?::[0-9.、,，\s]*)?|(?:资料|证据)\s*\d*)?$", re.IGNORECASE
)


def clean_streaming_answer(content: str) -> str:
    """清理流式中间文本，避免未闭合的内部证据标记短暂展示给员工。"""
    body = _MODEL_REFERENCE_SECTION.sub("", content)
    body = _MODEL_CITATION_PATTERN.sub("", body)
    body = _INTERNAL_MARKER_PATTERN.sub("", body)
    return _INCOMPLETE_INTERNAL_MARKER.sub("", body).rstrip()


def _selected_source_citations(content: str, results: list[SearchResult]) -> list[str]:
    grouped: dict[tuple[str, str, int | None], list[str]] = {}
    for match in _EVIDENCE_SELECTION_PATTERN.finditer(content):
        index = int(match.group(1)) - 1
        if not 0 <= index < len(results):
            continue
        result = results[index]
        available = extract_clause_references(result.content)
        requested = re.findall(r"\d+(?:\.\d+){1,3}", match.group(2) or "")
        # 模型只能选择当前片段中实际出现的条款，杜绝生成不存在的条款号。
        clauses = [clause for clause in requested if clause in available]
        if requested and not clauses:
            continue
        key = (result.document_id, result.title, result.page_number)
        selected = grouped.setdefault(key, [])
        for clause in clauses:
            if clause not in selected:
                selected.append(clause)
    citations: list[str] = []
    for (_, title, page_number), clauses in grouped.items():
        parts = [f"《{title}》"]
        if clauses:
            parts.append(f"第{'、'.join(clauses)}条")
        if page_number:
            parts.append(f"PDF第{page_number}页")
        citations.append(f"【{'｜'.join(parts)}】")
    return citations


def append_consolidated_citations(content: str, results: list[SearchResult]) -> str:
    """移除模型产生的行内标记，并在正文末尾追加系统生成的去重引用。"""
    citations = _selected_source_citations(content, results)
    body = _MODEL_REFERENCE_SECTION.sub("", content)
    body = _MODEL_CITATION_PATTERN.sub("", body)
    body = _INTERNAL_MARKER_PATTERN.sub("", body).strip()
    if not citations:
        citations = format_source_citations(results)
    if not citations:
        return body
    return body + "\n\n引用依据：\n" + "\n".join(f"- {item}" for item in citations)


class RagAgent:
    def __init__(self, settings: Settings, store: KnowledgeStore) -> None:
        self.settings = settings
        self.store = store

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.settings.embedding_api_key and self.settings.embedding_model)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.settings.llm_api_key and self.settings.llm_model)

    def _embedding_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.settings.embedding_api_key,
            base_url=self.settings.embedding_base_url,
            timeout=self.settings.embedding_timeout_seconds,
            max_retries=self.settings.embedding_max_retries,
        )

    def _llm_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            timeout=self.settings.embedding_timeout_seconds,
            max_retries=self.settings.embedding_max_retries,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embedding_enabled:
            raise RuntimeError("Embedding 尚未配置")
        vectors: list[list[float]] = []
        client = self._embedding_client()
        # DashScope text-embedding-v3 accepts at most 10 texts per request.
        batch_size = min(self.settings.embedding_batch_size, 10)
        for start in range(0, len(texts), batch_size):
            response = client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts[start : start + batch_size],
            )
            vectors.extend(
                item.embedding for item in sorted(response.data, key=lambda item: item.index)
            )
        if any(len(vector) != self.settings.embedding_dimensions for vector in vectors):
            raise RuntimeError("Embedding 服务返回的向量维度与 EMBEDDING_DIMENSIONS 不一致")
        return vectors

    def index_document(self, document_id: str) -> int:
        if not self.embedding_enabled:
            return 0
        model = self.settings.embedding_model
        assert model is not None
        self.store.set_embedding_status(document_id, "running")
        try:
            chunks = self.store.chunks_needing_embeddings(document_id, model)
            if not chunks:
                self.store.set_embedding_status(document_id, "ready")
                return 0
            vectors = self.embed([chunk.content for chunk in chunks])
            self.store.save_embeddings(
                [(chunk.id, vector) for chunk, vector in zip(chunks, vectors, strict=True)],
                model,
            )
            self.store.set_embedding_status(document_id, "ready")
            return len(chunks)
        except Exception as exc:
            self.store.set_embedding_status(document_id, "failed", str(exc)[:1000])
            raise

    def search(
        self,
        query: str,
        limit: int | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[SearchResult]:
        normalized = " ".join(query.split())[:500]
        if query_embedding is None and self.embedding_enabled:
            try:
                query_embedding = self.embed([normalized])[0]
            except Exception as exc:
                logger.warning("knowledge_embedding_unavailable", error_type=type(exc).__name__)
        results = self.store.search(normalized, limit or self.settings.rag_top_k, query_embedding)
        if query_embedding is None:
            return results
        return [result for result in results if result.score >= self.settings.rag_min_score]

    def answer(
        self,
        user_id: str,
        query: str,
        query_embedding: list[float] | None = None,
        stream_callback: Callable[[str], None] | None = None,
    ) -> AgentReply:
        history = []
        if self.llm_enabled and self.settings.rag_history_messages:
            try:
                history = self.store.recent_messages(user_id, self.settings.rag_history_messages)
            except Exception as exc:
                logger.warning("knowledge_history_unavailable", error_type=type(exc).__name__)
        self._save_history(user_id, "user", query[:2000])
        results = self.search(query, query_embedding=query_embedding)
        if not results:
            content = "知识库中没有足够依据，暂时无法可靠回答。"
        elif not self.llm_enabled:
            content = format_search_reply(query, results)
        else:
            context = "\n\n".join(
                f"[内部证据 E{index}]\n"
                f"资料名称：{result.title}\n"
                f"PDF页码：{result.page_number or '无'}\n"
                f"文内页码：{result.printed_page or '无'}\n"
                f"章节路径：{' / '.join(result.section_path) or '无'}\n"
                f"内容类型：{'表格' if result.element_type == 'table' else '正文'}\n"
                f"本片段可见条款：{'、'.join(extract_clause_references(result.content)) or '无'}\n"
                f"资料原文：\n{result.content}"
                for index, result in enumerate(results, start=1)
            )[: self.settings.rag_max_context_chars]
            try:
                content = self._generate_answer(
                    query, context, history, stream_callback=stream_callback
                )
                content = append_consolidated_citations(content, results)
                if stream_callback:
                    stream_callback(content)
            except Exception as exc:
                logger.warning("knowledge_generation_unavailable", error_type=type(exc).__name__)
                content = "归纳服务暂时不可用，以下为已发布资料原文：\n" + format_search_reply(
                    query, results
                )
        self._save_history(user_id, "assistant", content[:10000])
        return AgentReply(content=content, sources=results)

    def _save_history(self, user_id: str, role: str, content: str) -> None:
        try:
            self.store.add_message(user_id, role, content)
        except Exception as exc:
            logger.warning("knowledge_history_write_failed", error_type=type(exc).__name__)

    def _generate_answer(
        self,
        query: str,
        context: str,
        history: list[ChatMessage],
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        response = self._llm_client().chat.completions.create(
            model=self.settings.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业知识库助手。仅依据给定资料进行归纳回答；资料不足时"
                        "明确说明，不要编造。请将规定按主题归纳成简洁正文。每段涉及"
                        "制度事实的句末添加内部依据标记，格式为[E1:6.5.1]；引用多个条款"
                        "写成[E1:6.5.1、6.5.2]；片段没有条款号时写[E1]。E编号和条款号"
                        "必须来自下面对应的内部证据。不要输出资料名称、页码或引用依据区，"
                        "系统会移除内部标记并在回答末尾统一生成实际出处。\n\n" + context
                        + "\n\n"
                        + KNOWLEDGE_RESPONSE_GUIDELINES
                    ),
                },
                *[{"role": message.role, "content": message.content} for message in history],
                {"role": "user", "content": query},
            ],
            stream=bool(stream_callback),
        )
        if stream_callback:
            content = ""
            for chunk in response:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    content += delta
                    preview = clean_streaming_answer(content)
                    if preview:
                        stream_callback(preview)
            return content or "知识库中没有足够依据。"
        return response.choices[0].message.content or "知识库中没有足够依据。"


@lru_cache
def get_rag_agent() -> RagAgent:
    return RagAgent(get_settings(), get_knowledge_store())
