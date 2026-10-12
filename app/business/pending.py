"""待澄清槽位：员工回答了澄清问题之后，把上一轮已经确定的信息接回来。

澄清路径是 `create_plan -> clarification -> finalize_reply`，**不经过 execute_task**，
因此不会写入任何会话状态。结果是：系统提出了一个准确的澄清问题，却把已经从问句里
解析出来的产品型号和字段一起丢掉了；员工回答「材质」之后，系统只会重新索要型号。

这里只做一件事——把「已经确定的部分」存进一个槽位，下一轮接回来。
它不猜测、不扩展：只要出现新型号、或回答无法被明确解析成某个字段，就清空槽位，
避免陈旧状态影响后续提问。
"""

from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.business.intent import Intent, IntentDecision
from app.business.planner import PlannedTask, TaskPlan
from app.business.product_query import (
    ProductFieldRequest,
    ambiguity_choice,
    fallback_field_requests,
    product_query_from_dict,
    product_query_to_dict,
)
from app.business.product_selection import has_product_identifier

logger = structlog.get_logger()

# BusinessAgent 上的状态属性名。放在这里，使存储与读取两端只有一处定义。
STATE_ATTR = "_pending_clarifications"
SOURCE = "pending_clarification"


@dataclass(frozen=True)
class PendingClarification:
    """上一轮提出澄清时已经确定的信息。"""

    intent: str
    question: str
    product_query: dict[str, object] | None = None


def _state(agent: Any) -> Any:
    return getattr(agent, STATE_ATTR, None)


def clear(agent: Any, user_id: str) -> None:
    state = _state(agent)
    if state is None:
        return
    try:
        state.pop(user_id, None)
    except Exception as exc:  # noqa: BLE001 - 状态清理失败不应影响回答
        logger.warning("pending_clarification_clear_failed", error_type=type(exc).__name__)


def remember_product_query(
    agent: Any,
    user_id: str,
    product_query: Any,
    question: str,
    *,
    option_labels: tuple[str, ...] = (),
    intent: str = "product_query",
) -> None:
    """由执行层直接写入待澄清的产品查询。

    执行层（V2 校验）也会发现歧义，与计划层发现的是同一件事，因此共用这一个槽位。

    `option_labels` 是这次提问给出的选项（如「材质」「配件」）。把它们对应的字段也写进
    上下文，员工用位置指代回答（「后者」）时才有依据可用——那个词本身没有任何字段语义，
    按原话校验必然失败。

    产品查询现在只有正式 V2 链路，澄清结果统一写入正式待处理槽位。
    """
    state = _state(agent)
    if state is None:
        return
    option_fields = tuple(
        dict.fromkeys(
            request.field
            for label in option_labels
            for request in fallback_field_requests(label)
        )
    )
    if option_fields:
        product_query = replace(
            product_query,
            fields=tuple(dict.fromkeys((*product_query.fields, *option_fields))),
            requested_fields=tuple(
                dict.fromkeys(
                    (
                        *product_query.effective_field_requests(),
                        *(ProductFieldRequest(field, source="context") for field in option_fields),
                    )
                )
            ),
        )
    payload = product_query_to_dict(product_query)
    try:
        state[user_id] = {
            "intent": intent,
            "question": question,
            "product_query": payload,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending_clarification_write_failed", error_type=type(exc).__name__)
        return
    # 同时写进 V2 的「已校验上下文」。下一轮 V2 执行时把它当作 trusted_context，
    # 才能认定槽位里带来的型号引用有依据（引用来自上下文，而不是员工这一句原话）。
    context_state = getattr(agent, "_product_query_context", None)
    if context_state is not None:
        try:
            context_state[user_id] = {"plan": payload}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pending_clarification_context_write_failed", error_type=type(exc).__name__
            )


def remember(agent: Any, user_id: str, plan: TaskPlan) -> None:
    """记录本轮澄清时已经确定的信息，供下一轮接回。"""
    task = next((item for item in plan.tasks if item.product_query is not None), None)
    if task is None:
        return
    remember_product_query(
        agent,
        user_id,
        task.product_query,
        plan.clarification_question,
        option_labels=tuple(
            option
            for ambiguity in task.product_query.ambiguities
            for option in ambiguity.options
        ),
        intent=str(plan.primary_intent),
    )


def _load(agent: Any, user_id: str) -> dict[str, object] | None:
    state = _state(agent)
    if state is None:
        return None
    try:
        payload = state[user_id]
    except KeyError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending_clarification_read_failed", error_type=type(exc).__name__)
        return None
    return payload if isinstance(payload, dict) else None


def resolve(agent: Any, user_id: str, query: str) -> TaskPlan | None:
    """如果本句是在回答上一轮的澄清问题，返回可直接执行的计划。"""
    payload = _load(agent, user_id)
    if payload is None:
        return None
    # 出现新的产品型号说明员工换了话题，上一轮的澄清作废。
    if has_product_identifier(query):
        clear(agent, user_id)
        return None
    base = product_query_from_dict(payload.get("product_query"))
    choice = ambiguity_choice(query)
    if base is None or choice is None:
        clear(agent, user_id)
        return None

    resolved = replace(
        base,
        # 两种来源必须分开标：
        # - 型号来自槽位（上一轮），员工这一句没说，所以是 context；
        # - 字段来自员工**这一句回答**（「材质」/「配件」），所以是 fallback 并带原话。
        # 标错任一个都会被 V2 校验判为缺少依据：context 要求字段已在上下文里，
        # 而员工选的选项恰恰可能是上一轮没解析出来的那一项。
        references=tuple(
            replace(item, source="context") for item in base.effective_references()
        ),
        requested_fields=(ProductFieldRequest(choice, query.strip(), "context"),),
        fields=(choice,),
        original_fields=tuple(dict.fromkeys((*base.original_fields, choice))),
        context_mode="continuation",
        ambiguities=(),
    )
    clear(agent, user_id)
    decision = IntentDecision(
        intent=Intent.PRODUCT,
        confidence=0.99,
        reason="员工回答了上一轮的澄清问题",
        query_for_search=query.strip()[:500],
        source=SOURCE,
    )
    return TaskPlan(
        goal=f"{'、'.join(resolved.include_models) or query} {choice}",
        tasks=(PlannedTask(decision, resolved),),
        confidence=0.99,
        reason="接回上一轮澄清后确定的产品与字段",
        source=SOURCE,
        needs_clarification=False,
        clarification_question="",
    )
