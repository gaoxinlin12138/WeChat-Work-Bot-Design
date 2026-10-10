"""领域本体的覆盖一致性检查。

这不是抽象的设计洁癖，而是被反复验证过的缺陷类别。同一个 bug 在本项目出现过五次：

- 媒体角色表里有「配置图」，意图词表里没有 —— 于是「发我配置图」掉进通用问答；
- 域路由的客户问题词表有「换货」「催货」，话术场景本体里没有 —— 于是落到 unknown；
- 话术场景本体缺「最低」「报价」「回访」「发出」—— 同类词只写了一半；
- 制度话题词表存在两份且前者覆盖后者 —— 后者永远不可达。

这些缺陷的共同点：**它们不会让任何测试变红**，也不会让任何既有指标下降。只有当员工恰好
换一种说法提问时才会暴露。所以这里把「相邻词表必须互相覆盖」写成可执行的检查。

检查只做一件事：把一份词表里的每个词，交给该词表**下游**的分类器，看它有没有落空。
落空即漂移。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.business.intent import (
    _CONVERSATION_EXACT_QUERIES,
    _CUSTOMER_PROBLEM_WORDS,
    _POLICY_TOPIC_WORDS,
    _PRODUCT_OVERVIEW_WORDS,
    _REPLY_SCENARIO_RULES,
    MEDIA_ALIAS_CATALOG,
    Intent,
    ReplyScenario,
    classify_intent,
    classify_reply_scenario,
    media_type_and_role,
)
from app.business.product_query import ProductReference, is_overview_request

# 概览判定需要一个引用才能工作，这里用一个样例型号充当引用。
_OVERVIEW_SAMPLE_MODEL = "LY0653GDT"


def _overview_recognized(phrase: str) -> bool:
    return is_overview_request(
        f"{_OVERVIEW_SAMPLE_MODEL} {phrase}",
        (ProductReference(_OVERVIEW_SAMPLE_MODEL, "exact", _OVERVIEW_SAMPLE_MODEL),),
    )


@dataclass(frozen=True)
class CoverageCheck:
    """一份词表的每个词，都必须被它的下游分类器接住。"""

    name: str
    rationale: str
    words: tuple[str, ...]
    classify: Callable[[str], Any]
    acceptable: Callable[[Any], bool]


def find_uncovered(check: CoverageCheck) -> tuple[tuple[str, Any], ...]:
    """返回没有被下游接住的 (词, 下游给出的结果)。"""
    return tuple(
        (word, check.classify(word))
        for word in check.words
        if not check.acceptable(check.classify(word))
    )


def run_checks(
    checks: tuple[CoverageCheck, ...],
) -> dict[str, tuple[tuple[str, Any], ...]]:
    """返回每个检查的未覆盖词；全部通过时值为空元组。"""
    return {
        check.name: uncovered
        for check in checks
        for uncovered in (find_uncovered(check),)
    }


def _media_alias_words(*roles: str | None) -> tuple[str, ...]:
    return tuple(
        word
        for alias in MEDIA_ALIAS_CATALOG
        if alias.media_role in roles
        for word in alias.words
    )


def _matches_declared_alias(alias: Any, pair: tuple[str | None, str | None]) -> bool:
    """叫法必须映射回它声明时所属的类型；声明了角色的还要角色一致。

    只声明类型、角色为空的那一组（图片/照片/效果图/发张图）是「泛称」，允许下游
    解析出更具体的角色——例如「产品图片」含「产品图」，解析成产品图是正确的，
    不算漂移。因此只在声明了角色时才要求角色精确一致。
    """
    media_type, media_role = pair
    if media_type != alias.media_type:
        return False
    return alias.media_role is None or media_role == alias.media_role


ONTOLOGY_COVERAGE_CHECKS: tuple[CoverageCheck, ...] = (
    CoverageCheck(
        name="客户问题词表 → 话术场景",
        rationale=(
            "域路由用这些词判定「这是话术任务」，话术场景本体负责判断是哪一类。"
            "路由认识而场景不认识，就会落到 unknown，机器人反而回头追问员工场景。"
        ),
        words=_CUSTOMER_PROBLEM_WORDS,
        classify=classify_reply_scenario,
        acceptable=lambda label: label is not ReplyScenario.UNKNOWN,
    ),
    # 每个叫法都必须映射回它被声明时所属的角色。分成逐组检查，失败时能直接指出是哪一组。
    *(
        CoverageCheck(
            name=f"媒体本体 → 角色映射（{alias.media_role or '泛称'}）",
            rationale=(
                "媒体本体里声明的叫法，必须能被角色映射按声明解析出来；"
                "解析不出来或落到别的角色，说明两份媒体词表已经漂移。"
            ),
            words=alias.words,
            classify=media_type_and_role,
            acceptable=lambda pair, declared=alias: _matches_declared_alias(declared, pair),
        )
        for alias in MEDIA_ALIAS_CATALOG
    ),
    CoverageCheck(
        name="媒体本体 → 意图判定",
        rationale=(
            "媒体本体新增叫法时，意图判定必须同步认识，否则这句话会掉进通用问答，"
            "而不是进入产品媒体路径。"
        ),
        words=tuple(word for alias in MEDIA_ALIAS_CATALOG for word in alias.words),
        classify=classify_intent,
        acceptable=lambda intent: intent == Intent.PRODUCT_MEDIA,
    ),
    CoverageCheck(
        name="制度话题词表 → 意图判定",
        rationale="制度话题词表里的每个词都应当被判为制度意图。",
        words=_POLICY_TOPIC_WORDS,
        classify=classify_intent,
        acceptable=lambda intent: intent == Intent.POLICY,
    ),
    CoverageCheck(
        name="寒暄整句表 → 意图判定",
        rationale="整句寒暄表里的每一句都必须被判为普通对话，否则这条规则形同虚设。",
        words=_CONVERSATION_EXACT_QUERIES,
        classify=classify_intent,
        acceptable=lambda intent: intent == Intent.CONVERSATION,
    ),
    CoverageCheck(
        name="产品概览短语 → 概览判定",
        rationale=(
            "意图层用这些短语把提问判成「看整体」，概览判定（allow_overview）必须同样认识它们。"
            "否则「LY0653GDT 的全部参数」会被当成「没说要哪些字段」：旧链路会列出全部字段，"
            "V2 直接拒答。「参数」「规格」就这样漏过一次。"
        ),
        words=_PRODUCT_OVERVIEW_WORDS,
        classify=_overview_recognized,
        acceptable=lambda recognized: recognized is True,
    ),
    # 场景本体是「顺序即优先级」。某个词被声明在靠后的场景里，却被靠前的场景先接走，
    # 那么这条声明永远不可达——这正是两份制度词表那次事故的同一形态。
    *(
        CoverageCheck(
            name=f"场景本体 → {rule.scenario}（声明必须可达）",
            rationale=(
                "词被声明在本场景下，就必须真的被判为本场景；"
                "被判成别的场景说明它被靠前的规则抢先接走，声明不可达。"
            ),
            words=rule.topics,
            classify=classify_reply_scenario,
            acceptable=lambda label, expected=rule.scenario: label == expected,
        )
        for rule in _REPLY_SCENARIO_RULES
    ),
)
