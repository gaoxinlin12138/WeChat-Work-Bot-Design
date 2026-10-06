"""LangGraph subgraph for multi-turn customer reply collection."""

import re
import time
from functools import lru_cache
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.business.graph.state import (
    BusinessGraphContext,
    ReplySessionPayload,
    RoutedReplyPayload,
    reply_to_payload,
    session_from_payload,
    session_to_payload,
)
from app.business.intent import Intent, ReplyScenario
from app.business.tools.common import ReplySession, RoutedReply
from app.business.tools.reply_script import _SCENARIO_FIELDS, extract_reply_fields

ReplyStep = Literal["collect", "generate", "done"]


class ReplyScriptGraphState(TypedDict, total=False):
    query: str
    semantic_scenario: str | None
    session: ReplySessionPayload | None
    step: ReplyStep
    reply: RoutedReplyPayload


def load_reply_session(
    state: ReplyScriptGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    """Restore, reset or initialize a short-lived reply task."""
    from app.business import service

    agent = runtime.context.agent
    now = time.time()
    try:
        for key, value in list(agent._reply_sessions.items()):
            if now - value.last_seen > 1800:
                agent._state_pop(agent._reply_sessions, key)
    except Exception as exc:
        service.logger.warning("reply_session_cleanup_failed", error_type=type(exc).__name__)

    user_id = runtime.context.user_id
    stripped = state["query"].strip()
    if stripped in {"重置", "重新开始", "取消", "不用了"}:
        agent._state_pop(agent._reply_sessions, user_id)
        reply = RoutedReply(Intent.REPLY_SCRIPT, "已清除本次回复需求。请重新描述客户情况。", [])
        return {"step": "done", "reply": reply_to_payload(reply), "session": None}

    session = agent._state_get(agent._reply_sessions, user_id)
    if session is not None and not isinstance(session, ReplySession):
        service.logger.warning("reply_session_state_invalid", user_id_hash=hash(user_id))
        session = None
    scenario_value = state.get("semantic_scenario")
    detected = ReplyScenario(scenario_value) if scenario_value else ReplyScenario.UNKNOWN
    if session is None:
        if detected == ReplyScenario.UNKNOWN:
            reply = RoutedReply(
                Intent.REPLY_SCRIPT,
                "请告诉我客户属于哪种情况：产品咨询、报价议价、售后投诉、交付延期或客户跟进。也可以直接粘贴客户原话。",
                [],
                scenario=ReplyScenario.UNKNOWN,
            )
            return {"step": "done", "reply": reply_to_payload(reply), "session": None}
        session = ReplySession(detected)
        agent._reply_sessions[user_id] = session
        if len(agent._reply_sessions) > 500:
            agent._state_pop(agent._reply_sessions, next(iter(agent._reply_sessions)))
    elif detected not in (ReplyScenario.UNKNOWN, session.scenario):
        session = ReplySession(detected)
        agent._reply_sessions[user_id] = session
    session.last_seen = now
    agent._reply_sessions[user_id] = session
    return {"step": "collect", "session": session_to_payload(session)}


def route_after_load(state: ReplyScriptGraphState) -> Literal["collect", "done"]:
    return "done" if state["step"] == "done" else "collect"


def collect_reply_fields(
    state: ReplyScriptGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    """Merge obvious fields and decide whether another clarification is needed."""
    agent = runtime.context.agent
    session = session_from_payload(state.get("session"))
    if session is None:
        reply = RoutedReply(Intent.REPLY_SCRIPT, "回复任务状态已失效，请重新描述客户情况。", [])
        return {"step": "done", "reply": reply_to_payload(reply)}

    stripped = state["query"].strip()
    session.collected.update(extract_reply_fields(stripped, session.scenario))
    missing_keys = [
        key for key, _ in _SCENARIO_FIELDS[session.scenario] if key not in session.collected
    ]
    if missing_keys:
        extracted = extract_reply_fields(stripped, session.scenario)
        if session.scenario == ReplyScenario.PRICE_NEGOTIATION and "current_price" in missing_keys:
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

    agent._reply_sessions[runtime.context.user_id] = session
    follow_up = agent._reply_follow_up(session.scenario, session.collected)
    if follow_up.missing_fields:
        return {
            "step": "done",
            "session": session_to_payload(session),
            "reply": reply_to_payload(follow_up),
        }
    return {"step": "generate", "session": session_to_payload(session)}


def route_after_collection(state: ReplyScriptGraphState) -> Literal["generate", "done"]:
    return "generate" if state["step"] == "generate" else "done"


def generate_reply(
    state: ReplyScriptGraphState,
    runtime: Runtime[BusinessGraphContext],
) -> dict[str, object]:
    agent = runtime.context.agent
    session = session_from_payload(state.get("session"))
    if session is None:
        reply = RoutedReply(Intent.REPLY_SCRIPT, "回复任务状态已失效，请重新描述客户情况。", [])
        return {"step": "done", "reply": reply_to_payload(reply)}
    reply = agent._generate_reply_scripts(
        session.scenario, session.collected, state["query"].strip()
    )
    agent._state_pop(agent._reply_sessions, runtime.context.user_id)
    return {"step": "done", "reply": reply_to_payload(reply), "session": None}


@lru_cache(maxsize=1)
def get_reply_script_graph():
    graph = StateGraph(ReplyScriptGraphState, context_schema=BusinessGraphContext)
    graph.add_node("load_session", load_reply_session)
    graph.add_node("collect_fields", collect_reply_fields)
    graph.add_node("generate_reply", generate_reply)
    graph.add_edge(START, "load_session")
    graph.add_conditional_edges(
        "load_session",
        route_after_load,
        {"collect": "collect_fields", "done": END},
    )
    graph.add_conditional_edges(
        "collect_fields",
        route_after_collection,
        {"generate": "generate_reply", "done": END},
    )
    graph.add_edge("generate_reply", END)
    return graph.compile(name="reply-script")
