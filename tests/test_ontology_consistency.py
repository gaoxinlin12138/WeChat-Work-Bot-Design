"""本体一致性检查自身的测试。

两类断言缺一不可：
1. 真实本体当前是互相覆盖的；
2. 检查器**确实能发现漂移** —— 只验证「能通过」的检查器等于没有检查器。
"""

from app.business.domains import ONTOLOGY_COVERAGE_CHECKS, CoverageCheck, find_uncovered
from app.business.intent import Intent, ReplyScenario


def test_all_declared_ontologies_are_mutually_covered() -> None:
    """任何一处漂移都会在这里变成失败。

    这类缺陷不会让其它测试变红：曾经「配置图」只在角色映射里、不在意图词表里，
    「换货」「催货」只在域路由里、不在话术场景里，全都静默通过。
    """
    failures = {
        check.name: uncovered
        for check in ONTOLOGY_COVERAGE_CHECKS
        for uncovered in (find_uncovered(check),)
        if uncovered
    }

    assert failures == {}, f"本体出现漂移：{failures}"


def test_checker_detects_a_drifted_sibling_vocabulary() -> None:
    """检查器必须能发现「路由认识、下游不认识」这类漂移。"""
    drifted = CoverageCheck(
        name="构造的漂移样例",
        rationale="模拟域路由词表新增了一个下游场景本体没有覆盖的词。",
        words=("投诉", "一个下游完全不认识的说法"),
        classify=lambda word: ReplyScenario.AFTER_SALES_COMPLAINT
        if word == "投诉"
        else ReplyScenario.UNKNOWN,
        acceptable=lambda label: label is not ReplyScenario.UNKNOWN,
    )

    uncovered = find_uncovered(drifted)

    assert uncovered == (("一个下游完全不认识的说法", ReplyScenario.UNKNOWN),)


def test_checker_detects_a_shadowed_declaration() -> None:
    """顺序即优先级时，被靠前规则抢先接走的声明必须报出来。"""
    shadowed = CoverageCheck(
        name="构造的不可达声明",
        rationale="模拟某个词被声明在靠后场景，却被靠前场景先接走。",
        words=("发货",),
        # 「发货」被声明为交付延期，但下游实际判成了售后投诉
        classify=lambda _word: ReplyScenario.AFTER_SALES_COMPLAINT,
        acceptable=lambda label: label == ReplyScenario.DELIVERY_DELAY,
    )

    assert find_uncovered(shadowed) == (("发货", ReplyScenario.AFTER_SALES_COMPLAINT),)


def test_checker_accepts_covered_vocabulary() -> None:
    """不误报：下游正确接住时应当返回空。"""
    covered = CoverageCheck(
        name="构造的已覆盖样例",
        rationale="回执下游能正确处理的情况，确认检查器不会无条件报错。",
        words=("制度", "考勤"),
        classify=lambda _word: Intent.POLICY,
        acceptable=lambda intent: intent == Intent.POLICY,
    )

    assert find_uncovered(covered) == ()
