"""Knowledge-base and policy answer boundary."""

import re
from collections.abc import Callable
from typing import Any

from app.business.intent import IntentDecision, MediaAction
from app.business.tools.common import RoutedReply
from app.knowledge.service import format_source_citation


class KnowledgeToolMixin:
    @staticmethod
    def _is_detection_report_query(query: str) -> bool:
        normalized = re.sub(r"\s+", "", query)
        return "检测报告" in normalized or "检测文件" in normalized

    def _detection_report_reply(
        self, user_id: str, query: str, decision: IntentDecision
    ) -> RoutedReply | None:
        """Match detection-report metadata and expose the original PDF as a file source."""
        # Resolve through service at call time so the store is always the one the
        # caller injected. Calling the knowledge factory directly here bypassed
        # dependency injection and made offline evaluations read the real database.
        from app.business import service

        explicit_report_query = self._is_detection_report_query(query)
        try:
            store = service.get_rag_agent().store
        except RuntimeError:
            # Isolated evaluation environments intentionally disable the app
            # database and provide their own knowledge fixture.
            return None
        except Exception:
            # Knowledge metadata is optional for deployments that only expose
            # the business/product stores (including isolated unit tests).
            return (
                RoutedReply(decision.intent, "检测报告资料库暂时不可用，请稍后重试。", [])
                if explicit_report_query
                else None
            )
        choice_state = getattr(self, "_knowledge_choices", None)
        selected = re.fullmatch(r"(?:第\s*)?(\d+)(?:\s*(?:号|个|份))?", query.strip())
        choices = self._state_get(choice_state, user_id, []) if choice_state is not None else []
        document = None
        if selected and isinstance(choices, list):
            index = int(selected.group(1)) - 1
            if 0 <= index < len(choices):
                document = store.get_document(str(choices[index]["id"]))
                if choice_state is not None:
                    self._state_pop(choice_state, user_id)
        if document is None:
            matcher = getattr(store, "match_detection_reports", None)
            if not callable(matcher):
                # The active knowledge store has no detection-report index (an
                # offline fixture, for example). Fall through to normal retrieval
                # rather than claiming the report library is unavailable: ordinary
                # search can still find and cite the document.
                return None
            try:
                matches = matcher(query, limit=8)
            except Exception:
                return (
                    RoutedReply(decision.intent, "检测报告查询暂时不可用，请稍后重试。", [])
                    if explicit_report_query
                    else None
                )
            if not matches:
                return (
                    RoutedReply(
                        decision.intent,
                        "当前没有找到符合条件的已发布检测报告。请补充资料标题或文件名中的关键词后重试。",
                        [],
                    )
                    if explicit_report_query
                    else None
                )
            if len(matches) > 1:
                candidate_rows = [
                    {
                        "id": item.id,
                        "title": item.title,
                        "original_name": item.original_name,
                    }
                    for item in matches
                ]
                if choice_state is not None:
                    choice_state[user_id] = candidate_rows
                lines = ["找到多份检测报告，请回复编号选择："]
                candidate_sources = []
                for index, item in enumerate(candidate_rows, 1):
                    lines.append(f"{index}. {item['title']}（{item['original_name']}）")
                    candidate_sources.append(
                        {
                            "source_type": "document",
                            "source_id": item["id"],
                            "title": item["title"],
                            "original_name": item["original_name"],
                            "document_type": "检测报告",
                            "candidate": True,
                        }
                    )
                return RoutedReply(decision.intent, "\n".join(lines), candidate_sources)
            document = matches[0]
        if document is None:
            return None
        source = {
            "source_type": "document",
            "source_id": document.id,
            "title": document.title,
            "original_name": document.original_name,
            "media_type": "file",
            "media_path": str(store.stored_path(document.id)),
            "content_hash": document.content_hash,
            "document_type": "检测报告",
        }
        return RoutedReply(
            decision.intent,
            f"已找到检测报告：{document.title}（{document.original_name}），正在发送原 PDF。",
            [source],
            media_action=MediaAction.SEND,
        )

    def _answer_knowledge(
        self,
        user_id: str,
        original_query: str,
        search_query: str,
        decision: IntentDecision,
        prefetched_embedding: list[float] | None = None,
        stream_callback: Callable[[str], None] | None = None,
        **_: object,
    ) -> RoutedReply:
        # Resolve through service at call time so existing integrations that
        # patch the agent factory continue to work after the move.
        from app.business import service

        report_reply = self._detection_report_reply(user_id, original_query, decision)
        if report_reply is not None:
            return report_reply

        reply = service.get_rag_agent().answer(
            user_id,
            search_query,
            query_embedding=prefetched_embedding,
            stream_callback=stream_callback,
        )
        content = reply.content
        sources = reply.sources
        if re.search(r"没有足够依据|暂时无法可靠回答|未找到.*直接匹配|无法确认", content):
            # A refusal must not carry a generic top-k citation that can look
            # like evidence for the rejected question.
            content = re.sub(r"\n+引用依据[：:].*", "", content, flags=re.DOTALL).strip()
            sources = []
        return RoutedReply(
            decision.intent,
            content,
            [
                {
                    "source_type": "document",
                    "source_id": item.document_id,
                    "title": item.title,
                    "content": item.content,
                    "page_number": item.page_number,
                    "chunk_index": item.chunk_index,
                    "printed_page": item.printed_page,
                    "element_type": item.element_type,
                    "section_path": list(item.section_path),
                    "clause_numbers": list(item.clause_numbers),
                    "citation": format_source_citation(item),
                    "score": item.score,
                }
                for item in sources
            ],
        )


class KnowledgeTool(KnowledgeToolMixin):
    """Internal knowledge boundary."""

    name = "knowledge"

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        return self.owner._answer_knowledge(*args, **kwargs)
