"""统计并对比 app/ 里的关键词判定点，让目标里的「收敛关键词判定」可复现。

这个数字我引用错过两次：一次是拿两个不同的正则算出的结果做前后对比，一次是拿
「判定点」和「词表份数」当同一个东西。所以把它做成脚本，口径固定、可与 HEAD 对比。

口径说明（重要）：

- 本脚本数的是**判定点**——即「把查询文本和词表比较」的位置（`any(word in ...)` 等）。
- 它**不**数词表里的词。把内联词表提取成声明式本体**不会**让这个数字下降，
  因为判定点还在。要降低它必须**删除判定逻辑**，不是搬迁词表。
- 因此本指标只反映「判定逻辑的规模」，不反映「重复本体是否已消除」。后者靠审查
  同一概念是否只声明一次，脚本给不出。

用法：
    python -m scripts.count_keyword_sites
    python -m scripts.count_keyword_sites --compare-head
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 固定口径：判定点 = 查询文本与词表/模式的比较位置
SITE_PATTERN = re.compile(
    # 「in query」必须真的指向 query 本身，否则 for item in query.references 这类
    # 只是遍历的语句也会被算成关键词判定，把指标算高。
    r"in query(?![\w.])|in normalized(?![\w.])|in search_query(?![\w.])"
    r"|any\(word in|any\(term in|any\(marker in"
)
DEF_PATTERN = re.compile(r"^\s*(?:async )?def (\w+)")


def count_sites(text: str) -> int:
    return sum(1 for line in text.splitlines() if SITE_PATTERN.search(line))


def sites_by_function(text: str) -> dict[str, int]:
    current = "<module>"
    hits: dict[str, int] = {}
    for line in text.splitlines():
        match = DEF_PATTERN.match(line)
        if match:
            current = match.group(1)
        if SITE_PATTERN.search(line):
            hits[current] = hits.get(current, 0) + 1
    return hits


def head_version(relative_path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative_path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout if result.returncode == 0 else None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="统计 app/ 的关键词判定点")
    parser.add_argument("--compare-head", action="store_true", help="与 HEAD 版本对比")
    parser.add_argument("--by-function", action="store_true", help="按函数列出分布")
    args = parser.parse_args()

    files = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "app").rglob("*.py")
    )
    now_total = 0
    head_total = 0
    rows: list[tuple[str, int, int]] = []
    for relative in files:
        current_text = (ROOT / relative).read_text(encoding="utf-8")
        current = count_sites(current_text)
        now_total += current
        previous = 0
        if args.compare_head:
            original = head_version(relative)
            previous = count_sites(original) if original is not None else 0
            head_total += previous
        if current or previous:
            rows.append((relative, previous, current))
        if args.by_function and current:
            print(f"{relative}  共 {current}")
            for name, count in sorted(
                sites_by_function(current_text).items(), key=lambda item: -item[1]
            ):
                print(f"    {name:<42} {count}")

    if args.compare_head:
        delta = now_total - head_total
        print(f"关键词判定点：HEAD {head_total} → 现在 {now_total}（净变化 {delta:+d}）")
        print()
        print(f"{'文件':<48}{'前':>5}{'后':>6}{'变化':>7}")
        for relative, previous, current in sorted(rows, key=lambda row: row[1] - row[2]):
            if previous != current:
                print(f"{relative:<48}{previous:>5}{current:>6}{current - previous:>+7}")
    else:
        print(f"关键词判定点合计：{now_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
