"""领域边界与本体一致性。

`consistency` 把「相邻词表必须互相覆盖」做成可执行检查：这类漂移在本项目出现过五次，
且从不触发测试失败。
"""

from app.business.domains.consistency import (
    ONTOLOGY_COVERAGE_CHECKS,
    CoverageCheck,
    find_uncovered,
    run_checks,
)

__all__ = [
    "ONTOLOGY_COVERAGE_CHECKS",
    "CoverageCheck",
    "find_uncovered",
    "run_checks",
]
