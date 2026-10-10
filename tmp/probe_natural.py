"""拿一批「员工真会这么问」的话去跑，统计现在还会挂多少、挂在哪儿。

为什么写这个：前面每次都是「你问一句 → 我改一处」。
这里主动列出剩余的失败面，用数量代替保证。
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from scripts.readonly_env import ReadOnlyBusiness  # noqa: E402

REFUSAL_MARKERS = (
    "无法安全执行",
    "无法可靠确认",
    "暂时做不了",
    "没有找到对应产品",
    "请先提供具体产品型号",
    "这个查询条件",
)


def classify(reply) -> str:
    text = str(reply.content)
    if any(marker in text for marker in REFUSAL_MARKERS):
        return "拒答"
    if text.lstrip().startswith("请问") or "请补充明确" in text:
        return "反问"
    return "作答"


# 单轮：(问题, 期望答复类型)
SINGLE = [
    ("灰玻璃·枪灰颜色的产品有哪些", "作答"),  # 带"颜色"二字
    ("灰玻璃·枪灰产品有哪些", "作答"),  # 不带字段词，值被原样引用
    ("灰玻璃枪灰的产品有哪些", "作答"),  # 员工漏打间隔号 ← 值不再逐字匹配
    ("全铜的有哪些", "作答"),  # 材质值被引用
    ("材质是全铜的产品有哪些", "作答"),  # 带字段词
    ("镜面玻璃·镜面是哪三款", "作答"),  # 承接式追问
    ("最便宜的是哪个", "作答"),  # 极值
    ("最贵的套装价是哪个", "作答"),  # 极值
    ("有几个系列", "作答"),  # 去重计数
    ("镜面玻璃·电镀的有几种", "作答"),  # 带筛选的计数
    ("价格在150左右的产品", "作答"),  # 数值区间
    ("300块以内的有哪些", "作答"),  # 数值上界
    ("LY0653GDT 的备注是什么", "作答"),  # 备注字段
    ("套装价最高的前三名是哪些", "作答"),  # 排名（尚未实现的能力）
]

# 多轮：(会话, 最后一个问题, 期望)
MULTI = [
    (["现在的产品里，一共有多少种颜色", "那三款多少钱"], "承接追问·不重复字段"),
    (["镜面玻璃·镜面是哪三款", "第二个多少钱"], "承接追问·序数"),
    (["LY0653GDT", "它的材质是什么"], "承接追问·它"),
]


def main() -> int:
    business = ReadOnlyBusiness(v2_mode="on", with_planner=True)
    failures: list[str] = []
    total = 0

    print("=" * 70)
    print("单轮")
    print("=" * 70)
    for index, (question, expected) in enumerate(SINGLE, 1):
        total += 1
        plan, reply = business.invoke(index, question)
        actual = classify(reply)
        task = plan.tasks[0] if plan.tasks else None
        query = task.product_query if task else None
        shape = ""
        if query is not None:
            parts = [query.operation]
            if query.filters:
                parts.append("筛选" + str(len(query.filters)))
            if query.aggregate is not None:
                parts.append(f"聚合:{query.aggregate.kind}")
            shape = "/".join(parts)
        mark = "OK  " if actual == expected else "FAIL"
        if actual != expected:
            failures.append(f"{question} → {actual}（期望{expected}）")
        head = str(reply.content).strip().splitlines()[0][:44]
        print(f"  {mark} {question[:26]:<28} 判={actual:<4} 计划={shape:<18} {head}")

    print()
    print("=" * 70)
    print("多轮（承接追问）")
    print("=" * 70)
    for index, (turns, label) in enumerate(MULTI, 100):
        total += 1
        results = business.invoke_turns(index, turns)
        plan, reply = results[-1]
        actual = classify(reply)
        mark = "OK  " if actual == "作答" else "FAIL"
        if actual != "作答":
            failures.append(f"{label}（{turns[-1]}）→ {actual}")
        head = str(reply.content).strip().splitlines()[0][:40]
        print(f"  {mark} {label:<12} 判={actual:<4} {head}")

    print()
    print("=" * 70)
    print(f"合计 {total} 条，失败 {len(failures)} 条")
    for item in failures:
        print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
