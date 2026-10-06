"""Customer-reply scenario collection, retrieval and generation."""

import re
import time

import structlog

from app.business.intent import Intent, ReplyScenario
from app.business.store import BusinessResult
from app.business.tools.common import ReplySession, RoutedReply
from app.knowledge.service import format_source_citation

logger = structlog.get_logger()

_SCENARIO_LABELS = {
    ReplyScenario.PRODUCT_INQUIRY: "产品咨询",
    ReplyScenario.PRICE_NEGOTIATION: "报价议价",
    ReplyScenario.AFTER_SALES_COMPLAINT: "售后投诉",
    ReplyScenario.DELIVERY_DELAY: "交付延期",
    ReplyScenario.CUSTOMER_FOLLOW_UP: "客户跟进",
}
_SCENARIO_FIELDS: dict[ReplyScenario, tuple[tuple[str, str], ...]] = {
    ReplyScenario.PRODUCT_INQUIRY: (
        ("product", "产品名称或型号"),
        ("customer_message", "客户原话或具体需求"),
    ),
    ReplyScenario.PRICE_NEGOTIATION: (
        ("product", "产品名称或型号"),
        ("quantity", "预计采购数量"),
        ("current_price", "当前报价（不知道可填‘未知’）"),
        ("customer_message", "客户原话或目标价格"),
    ),
    ReplyScenario.AFTER_SALES_COMPLAINT: (
        ("product", "产品名称或型号"),
        ("issue", "问题现象"),
        ("customer_request", "客户希望如何处理"),
    ),
    ReplyScenario.DELIVERY_DELAY: (
        ("product", "产品名称或型号"),
        ("delivery_status", "目前交付进度或预计时间"),
        ("customer_request", "客户最晚需要时间或希望的处理方式"),
    ),
    ReplyScenario.CUSTOMER_FOLLOW_UP: (
        ("product", "涉及的产品或项目"),
        ("customer_message", "上次沟通内容或客户当前疑虑"),
    ),
}
_REPLY_BANNED_PHRASES = (
    "感谢您的理解与耐心",
    "感谢您的反馈",
    "我们已收到您的需求",
    "正在确认可协调的空间",
    "为您提供更合适的方案",
    "第一时间",
    "敬请谅解",
    "内部确认",
    "内部流程",
    "模型",
    "资料编号",
)
_NEW_TASK_MARKERS = (
    "查一下",
    "查询",
    "查查",
    "查一查",
    "制度",
    "规定",
    "考勤",
    "请假",
    "报销",
    "参数",
    "规格",
    "产品价格",
    "多少钱",
    "图片",
    "照片",
    "视频",
    "发我",
    "播放",
    "帮我查",
    "帮我写",
    "怎么回复",
    "生成回复",
    "回复话术",
)


def _looks_like_product(text: str) -> bool:
    pattern = r"[A-Za-zＡ-Ｚa-zａ-ｚ]*[-－]?[A-Za-zＡ-Ｚa-zａ-ｚ0-9０-９]{2,}"
    return bool(re.search(pattern, text)) or any(word in text for word in ("花洒", "套装", "项目"))


def extract_reply_fields(text: str, scenario: ReplyScenario) -> dict[str, str]:
    """从员工自然语言提取明显话术字段。"""
    values: dict[str, str] = {}
    if _looks_like_product(text):
        code = re.search(r"[A-Za-zＡ-Ｚa-zａ-ｚ]{0,8}[-－]?[A-Za-zＡ-Ｚa-zａ-ｚ0-9０-９]{2,}", text)
        values["product"] = (
            code.group(0)
            if code
            else next(word for word in ("花洒", "套装", "项目") if word in text)
        )
    quantity = re.search(r"(\d+(?:\.\d+)?)\s*(台|套|个|件|箱|批|吨|组)", text)
    if quantity:
        values["quantity"] = quantity.group(0)
    price = re.search(r"(\d+(?:\.\d+)?)\s*(元|块)", text)
    if price:
        values["current_price"] = price.group(0)
    if "客户" in text or "客户原话" in text or "客户说" in text:
        values["customer_message"] = text[:500]
    if scenario == ReplyScenario.AFTER_SALES_COMPLAINT:
        if any(word in text for word in ("坏", "故障", "漏", "不出水", "质量", "投诉")):
            values["issue"] = text[:300]
        if any(word in text for word in ("退", "换", "修", "赔", "处理")):
            values["customer_request"] = text[:300]
    if scenario == ReplyScenario.DELIVERY_DELAY and any(
        word in text for word in ("发货", "交期", "延期", "延迟", "到货", "进度")
    ):
        values["delivery_status"] = text[:300]
    if any(word in text for word in ("希望", "要求", "最晚", "想要", "怎么处理")):
        values["customer_request"] = text[:300]
    return values


class ReplyScriptToolMixin:
    @staticmethod
    def _business_settings():
        from app.business import service

        return service.get_settings()

    @staticmethod
    def _business_rag_agent():
        from app.business import service

        return service.get_rag_agent()

    def _reply_follow_up(self, scenario: ReplyScenario, collected: dict[str, str]) -> RoutedReply:
        missing = [label for key, label in _SCENARIO_FIELDS[scenario] if key not in collected]
        if not missing:
            return RoutedReply(Intent.REPLY_SCRIPT, "", [], scenario=scenario)
        numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(missing, 1))
        hint = ""
        if scenario == ReplyScenario.AFTER_SALES_COMPLAINT:
            hint = "例如可补充：客户想退货、换货、维修，或需要先核实。"
        elif scenario == ReplyScenario.PRICE_NEGOTIATION:
            hint = "例如可直接补充：采购数量、当前报价或客户目标价。"
        return RoutedReply(
            Intent.REPLY_SCRIPT,
            (
                f"这是“{_SCENARIO_LABELS[scenario]}”场景。为了生成合适的客户回复，还需要："
                f"\n{numbered}\n\n{hint or '可一次回复一项或一次补充多项。'}"
            ),
            [],
            scenario=scenario,
            missing_fields=missing,
        )

    @staticmethod
    def _reply_risks(scenario: ReplyScenario, context: str) -> list[str]:
        risks: list[str] = []
        if scenario == ReplyScenario.PRICE_NEGOTIATION:
            risks.append("未确认内部授权前，不要在回复中承诺最低价或具体折扣。")
        if scenario == ReplyScenario.DELIVERY_DELAY:
            risks.append("交期未被资料明确确认时，建议使用‘预计’或‘正在确认’，不要承诺确定日期。")
        if scenario == ReplyScenario.AFTER_SALES_COMPLAINT:
            risks.append("退款、赔偿和免费更换需依据售后政策或内部审批，不要直接承诺。")
        return risks

    def _generate_reply_scripts(
        self, scenario: ReplyScenario, collected: dict[str, str], query: str
    ) -> RoutedReply:
        context = "；".join(f"{key}：{value}" for key, value in collected.items())
        search_query = f"{_SCENARIO_LABELS[scenario]} {context} {query}"[:1000]
        settings = self._business_settings()
        query_embedding: list[float] | None = None
        if getattr(settings, "embedding_api_key", None) and getattr(
            settings, "embedding_model", None
        ):
            try:
                query_embedding = self._business_rag_agent().embed([search_query])[0]
            except Exception as exc:
                logger.warning("reply_script_embedding_failed", error_type=type(exc).__name__)
        results: list[BusinessResult] = []
        try:
            results.extend(
                self.store.search_scripts(
                    search_query, limit=3, audience="customer", query_embedding=query_embedding
                )
            )
        except Exception as exc:
            logger.warning(
                "reply_script_search_failed", source="scripts", error_type=type(exc).__name__
            )
        try:
            results.extend(
                self.store.search_products(search_query, limit=3, query_embedding=query_embedding)
            )
        except Exception as exc:
            logger.warning(
                "reply_script_search_failed", source="products", error_type=type(exc).__name__
            )
        try:
            policy_results = self._business_rag_agent().search(
                search_query, limit=5, query_embedding=query_embedding
            )
        except Exception as exc:
            logger.warning(
                "reply_script_search_failed", source="policy", error_type=type(exc).__name__
            )
            policy_results = []
        evidence_items = [
            *(f"资料：{item.title}\n{item.content[:700]}" for item in results[:5]),
            *(
                f"政策资料：{item.title}（PDF第{item.page_number or '无'}页）\n{item.content}"
                for item in policy_results
            ),
        ]
        evidence = "\n\n".join(evidence_items)
        content = ""
        if settings.llm_api_key and settings.llm_model:
            prompt = (
                "你是企业微信客服话术助手。请只回答客户当前最关心的问题，输出三条短消息，标题严格使用【直接回复版】、【更简短版】、【稍微亲和版】。每条只写一到两句，控制在80个汉字以内，员工可以直接复制给客户。只使用员工已提供或资料明确给出的事实，不要主动介绍无关参数，不要编造卖点。不能承诺未经确认的折扣、交期、退款或赔偿。不要写感谢理解、第一时间、正在确认可协调空间、内部流程等套话；信息不确定时只说‘我帮您核一下，确认后回复您’。不要输出分析、解释、资料编号或风险提示。\n\n"
                f"场景：{_SCENARIO_LABELS[scenario]}\n员工已提供：{context}\n"
                f"参考资料：{evidence or '暂无'}"
            )
            try:
                response = (
                    self._business_rag_agent()
                    ._llm_client()
                    .chat.completions.create(
                        model=settings.llm_model,
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": query},
                        ],
                    )
                )
                content = response.choices[0].message.content or ""
            except Exception as exc:
                logger.warning("reply_script_generation_failed", error_type=type(exc).__name__)
        product = collected.get("product", "该产品")
        if not content or not self._reply_quality_ok(content):
            content = self._fallback_reply(scenario, product, collected)
        risks = self._reply_risks(scenario, context)
        if risks:
            content += "\n\n风险提示：\n" + "\n".join(f"- {risk}" for risk in risks)
        if policy_results:
            content += "\n\n内部参考依据（仅供核对，请勿整段转发给客户）：\n" + "\n".join(
                f"- {format_source_citation(item)}" for item in policy_results
            )
        source_dicts = [self.store.as_dict(item) for item in results[:8]]
        source_dicts.extend(
            {
                "source_type": "document",
                "source_id": item.document_id,
                "title": item.title,
                "page_number": item.page_number,
                "chunk_index": item.chunk_index,
                "printed_page": item.printed_page,
                "element_type": item.element_type,
                "section_path": list(item.section_path),
                "citation": format_source_citation(item),
                "score": item.score,
            }
            for item in policy_results
        )
        return RoutedReply(
            Intent.REPLY_SCRIPT, content, source_dicts, scenario=scenario, risk_warnings=risks
        )

    @staticmethod
    def _reply_quality_ok(content: str) -> bool:
        if any(phrase in content for phrase in _REPLY_BANNED_PHRASES):
            return False
        required = ("【直接回复版】", "【更简短版】", "【稍微亲和版】")
        if not all(title in content for title in required):
            return False
        sections = re.split(r"【(?:直接回复版|更简短版|稍微亲和版)】", content)[1:]
        return bool(sections) and all(1 <= len(section.strip()) <= 180 for section in sections)

    @staticmethod
    def _fallback_reply(scenario: ReplyScenario, product: str, collected: dict[str, str]) -> str:
        quantity, price = collected.get("quantity", ""), collected.get("current_price", "")
        price_text = (
            f"目前报价是{price}/套。" if price and price != "未知" else "价格我再帮您核一下。"
        )
        if scenario == ReplyScenario.PRICE_NEGOTIATION:
            return (
                f"【直接回复版】\n您好，{product}{price_text}您要{quantity or '这批'}，"
                f"我再帮您按数量核一下价格，确认后回复您。\n\n【更简短版】\n"
                f"{product}{price_text}我再按{quantity or '采购数量'}帮您核一下，确认后回复您。\n\n"
                f"【稍微亲和版】\n您好，收到您的议价需求了。{product}我再帮您核一下，确认后回复您。"
            )
        if scenario == ReplyScenario.DELIVERY_DELAY:
            return (
                f"【直接回复版】\n您好，{product}的发货进度我正在帮您核实，确认后回复您。\n\n"
                f"【更简短版】\n{product}交期我再确认一下，有结果后回复您。\n\n"
                f"【稍微亲和版】\n您好，我帮您跟进一下{product}的发货时间，确认后马上回复您。"
            )
        if scenario == ReplyScenario.AFTER_SALES_COMPLAINT:
            return (
                f"【直接回复版】\n您好，{product}的问题麻烦您发一下现场照片和订单信息，我先帮您核实处理。\n\n"
                f"【更简短版】\n麻烦发一下{product}的问题照片和订单信息，我帮您核实。\n\n"
                f"【稍微亲和版】\n您好，给您添麻烦了。您把{product}的问题照片发我，我来跟进处理。"
            )
        if scenario == ReplyScenario.CUSTOMER_FOLLOW_UP:
            return (
                f"【直接回复版】\n您好，想跟进一下{product}的进展，您这边还有需要确认的问题吗？\n\n"
                f"【更简短版】\n您好，{product}考虑得怎么样了？还有问题我可以继续协助。\n\n"
                f"【稍微亲和版】\n您好，之前咨询的{product}还在考虑吗？有需要我再帮您看看。"
            )
        return (
            f"【直接回复版】\n您好，关于{product}，请告诉我您的使用场景和数量，我帮您确认。\n\n"
            f"【更简短版】\n您好，请告诉我{product}的使用场景和数量。\n\n"
            f"【稍微亲和版】\n您好，我帮您看看{product}是否合适，您主要关注哪些方面？"
        )

    def _answer_reply_script(
        self, user_id: str, query: str, semantic_scenario: ReplyScenario | None = None
    ) -> RoutedReply:
        now = time.time()
        try:
            for key, value in list(self._reply_sessions.items()):
                if now - value.last_seen > 1800:
                    self._state_pop(self._reply_sessions, key)
        except Exception as exc:
            logger.warning("reply_session_cleanup_failed", error_type=type(exc).__name__)
        stripped = query.strip()
        if stripped in {"重置", "重新开始", "取消", "不用了"}:
            self._state_pop(self._reply_sessions, user_id)
            return RoutedReply(Intent.REPLY_SCRIPT, "已清除本次回复需求。请重新描述客户情况。", [])
        session = self._state_get(self._reply_sessions, user_id)
        if session is not None and not isinstance(session, ReplySession):
            logger.warning("reply_session_state_invalid", user_id_hash=hash(user_id))
            session = None
        detected = semantic_scenario or ReplyScenario.UNKNOWN
        if session is None:
            if detected == ReplyScenario.UNKNOWN:
                return RoutedReply(
                    Intent.REPLY_SCRIPT,
                    "请告诉我客户属于哪种情况：产品咨询、报价议价、售后投诉、交付延期或客户跟进。也可以直接粘贴客户原话。",
                    [],
                    scenario=ReplyScenario.UNKNOWN,
                )
            session = ReplySession(detected)
            self._reply_sessions[user_id] = session
            if len(self._reply_sessions) > 500:
                self._state_pop(self._reply_sessions, next(iter(self._reply_sessions)))
        elif detected not in (ReplyScenario.UNKNOWN, session.scenario):
            session = ReplySession(detected)
            self._reply_sessions[user_id] = session
        session.last_seen = now
        self._reply_sessions[user_id] = session
        session.collected.update(extract_reply_fields(stripped, session.scenario))
        missing_keys = [
            key for key, _ in _SCENARIO_FIELDS[session.scenario] if key not in session.collected
        ]
        if missing_keys:
            extracted = extract_reply_fields(stripped, session.scenario)
            if (
                session.scenario == ReplyScenario.PRICE_NEGOTIATION
                and "current_price" in missing_keys
            ):
                numbers = re.findall(r"\d+(?:\.\d+)?", stripped)
                quantity_value = re.search(
                    r"(\d+(?:\.\d+)?)\s*(?:台|套|个|件|箱|批|吨|组)", stripped
                )
                quantity_number = quantity_value.group(1) if quantity_value else None
                product_numbers = set(
                    re.findall(r"\d+(?:\.\d+)?", session.collected.get("product", ""))
                )
                candidates = [
                    number
                    for number in numbers
                    if number != quantity_number and number not in product_numbers
                ]
                if candidates:
                    session.collected["current_price"] = candidates[-1] + "元"
                    missing_keys = [key for key in missing_keys if key != "current_price"]
            if stripped in {"未知", "不知道", "不清楚"}:
                session.collected[missing_keys[0]] = stripped
            elif len(missing_keys) == 1 and not extracted:
                session.collected[missing_keys[0]] = stripped[:500]
        self._reply_sessions[user_id] = session
        follow_up = self._reply_follow_up(session.scenario, session.collected)
        if follow_up.missing_fields:
            return follow_up
        reply = self._generate_reply_scripts(session.scenario, session.collected, stripped)
        self._state_pop(self._reply_sessions, user_id)
        return reply

    def index_published_reply_scripts(self) -> int:
        settings = self._business_settings()
        if not getattr(settings, "embedding_api_key", None) or not getattr(
            settings, "embedding_model", None
        ):
            return 0
        pending = self.store.reply_scripts_needing_embeddings(settings.embedding_model)
        if not pending:
            return 0
        vectors = self._business_rag_agent().embed([item["search_text"] for item in pending])
        self.store.save_reply_script_embeddings(
            [
                (item["id"], item["search_text"], vector)
                for item, vector in zip(pending, vectors, strict=True)
            ],
            settings.embedding_model,
        )
        return len(pending)
