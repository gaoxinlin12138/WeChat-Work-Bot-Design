"""WeCom-safe answer layout helpers shared by deterministic and model replies."""

import re
from collections.abc import Iterable

KNOWLEDGE_RESPONSE_GUIDELINES = (
    "回答格式要求：先直接回答结论，再补充必要细节。单个对象且字段不超过三个时使用短句或键值行；"
    "多个同类对象、清单或对比结果使用编号分组，每项先写名称，再用“字段：值｜字段：值”展示"
    "最多四个核心字段，更多字段换行。操作流程使用编号步骤，制度解释使用“结论、依据、注意事项”"
    "结构，客服话术保持可直接复制的短消息。不要输出 Markdown 管道表格、HTML 表格、JSON、代码块、"
    "空字段或内部推理；缺失数据明确写“未登记”，不得猜测。"
)

_MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def format_key_value_lines(
    fields: Iterable[tuple[str, str]],
    *,
    max_fields_per_line: int = 4,
) -> list[str]:
    """Render compact mobile-friendly key/value rows without relying on table support."""
    cells = [
        f"{label}：{' '.join(str(value).split())}"
        for label, value in fields
        if str(value).strip()
    ]
    return [
        "｜".join(cells[start : start + max_fields_per_line])
        for start in range(0, len(cells), max_fields_per_line)
    ]


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_rows(headers: list[str], rows: list[list[str]]) -> list[str]:
    rendered: list[str] = []
    for index, row in enumerate(rows, 1):
        values = [*row, *([""] * max(0, len(headers) - len(row)))]
        if rendered:
            rendered.append("")
        first_label = headers[0] or "项目"
        first_value = values[0] or f"第{index}项"
        rendered.append(f"{index}. {first_label}：{first_value}")
        rendered.extend(
            format_key_value_lines(
                [
                    (header, value)
                    for header, value in zip(headers[1:], values[1:], strict=False)
                    if header and value
                ]
            )
        )
    return rendered


def normalize_wecom_answer(content: str) -> str:
    """Convert unsupported Markdown tables to stable WeCom key/value blocks."""
    lines = content.splitlines()
    normalized: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or not _MARKDOWN_TABLE_SEPARATOR.fullmatch(
            lines[index + 1]
        ):
            normalized.append(lines[index])
            index += 1
            continue

        headers = _split_table_row(lines[index])
        row_index = index + 2
        rows: list[list[str]] = []
        while row_index < len(lines) and "|" in lines[row_index] and lines[row_index].strip():
            rows.append(_split_table_row(lines[row_index]))
            row_index += 1
        if len(headers) < 2 or not rows:
            normalized.append(lines[index])
            index += 1
            continue
        normalized.extend(_render_table_rows(headers, rows))
        index = row_index
    return "\n".join(normalized).strip()
