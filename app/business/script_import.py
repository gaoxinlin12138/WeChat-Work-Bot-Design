"""回复话术文档导入：把常见办公文档拆成可审核的独立话术。"""

import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from defusedxml import ElementTree
from pypdf import PdfReader

MAX_IMPORT_CASES = 100
MAX_IMPORT_TEXT = 200_000


class ImportLimitError(ValueError):
    """The import is valid in shape but too large to process in one request."""


def validate_import_size(scripts: list[dict[str, object]]) -> None:
    if len(scripts) > MAX_IMPORT_CASES:
        raise ImportLimitError(f"每次最多导入 {MAX_IMPORT_CASES} 条话术，请拆分文件后上传")
    total = 0
    for item in scripts:
        question = str(item.get("customer_question") or "")
        answer = str(item.get("employee_reply") or item.get("content") or "")
        if len(question) > 2000 or len(answer) > 10000:
            raise ImportLimitError("单条客户问题最多 2000 字，员工回复最多 10000 字")
        total += len(question) + len(answer)
        total += sum(
            len(str(value))
            for name, value in item.items()
            if name not in {"customer_question", "employee_reply", "content"}
        )
    if total > MAX_IMPORT_TEXT:
        raise ImportLimitError(f"单次话术总文本不能超过 {MAX_IMPORT_TEXT} 字，请拆分文件")


def _checked_archive(data: bytes) -> ZipFile:
    archive = ZipFile(BytesIO(data))
    members = archive.infolist()
    if len(members) > 200:
        archive.close()
        raise ValueError("压缩包文件数量超过限制")
    total = 0
    for member in members:
        name = Path(member.filename)
        if name.is_absolute() or ".." in name.parts:
            archive.close()
            raise ValueError("压缩包包含非法路径")
        total += member.file_size
        if member.file_size > 20 * 1024 * 1024 or total > 100 * 1024 * 1024:
            archive.close()
            raise ValueError("压缩包解压后内容超过限制")
    return archive


def _read_docx(data: bytes) -> str:
    with _checked_archive(data) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _xlsx_rows(data: bytes) -> list[list[str]]:
    with _checked_archive(data) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()).strip() for node in root]
        sheet_name = next(
            name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")
        )
        root = ElementTree.fromstring(archive.read(sheet_name))
    rows = []
    for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        cells = []
        for cell in row:
            if cell.tag.endswith("}c"):
                value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter() if node.tag.endswith("}t")
                    )
                # Spreadsheet XML omits empty cells; retain their actual column positions.
                letters = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
                column = 0
                if letters:
                    for letter in letters.group():
                        column = column * 26 + ord(letter) - ord("A") + 1
                    column -= 1
                else:
                    column = len(cells)
                if column > 100:
                    raise ImportLimitError("Excel 列数超过限制，请只保留客户问题和员工回复等必要列")
                cells.extend([""] * (column + 1 - len(cells)))
                cells[column] = value.strip()
        if any(cells):
            rows.append(cells)
            if len(rows) > MAX_IMPORT_CASES + 1:
                raise ImportLimitError(f"Excel 每次最多支持 {MAX_IMPORT_CASES} 行话术")
    return rows


def _read_xlsx(data: bytes) -> str:
    return "\n".join(" | ".join(row) for row in _xlsx_rows(data))


def extract_text(filename: str, data: bytes) -> str:
    extension = Path(filename).suffix.lower()
    if extension in {".txt", ".md"}:
        return data.decode("utf-8-sig", errors="replace")
    if extension == ".docx":
        return _read_docx(data)
    if extension == ".xlsx":
        return _read_xlsx(data)
    if extension == ".pdf":
        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("话术文件仅支持 PDF、DOCX、XLSX、TXT、MD")


_HEADING = re.compile(
    r"^(?:#{1,6}\s*|[一二三四五六七八九十百]+[、.)．]|\d+[、.)．])"
    r"(.+?)\s*$"
)
_META = {
    "场景": "scenario",
    "适用场景": "scenario",
    "适用条件": "conditions",
    "必须信息": "required_fields",
    "必填信息": "required_fields",
    "禁止表达": "forbidden_phrases",
    "禁止承诺": "forbidden_phrases",
    "适用产品": "product_scope",
}

_COMMON_FORBIDDEN = ("保证明天发货", "承诺最低价", "内部资料编号", "正在确认可协调空间")


def validation_warnings(script: dict[str, object]) -> list[str]:
    content = str(script.get("content") or "")
    warnings = []
    if len(content) > 500:
        warnings.append("话术较长，建议压缩为一到两句")
    if not str(script.get("scenario") or "").strip() or str(script.get("scenario")) == "未分类":
        warnings.append("尚未明确使用场景")
    for phrase in _COMMON_FORBIDDEN:
        if phrase in content:
            warnings.append(f"包含风险表达：{phrase}")
    variables = re.findall(r"\{([^{}]+)\}", content)
    declared = {str(item).strip() for item in script.get("variables", []) or []}
    undeclared = [item for item in variables if item not in declared]
    if undeclared:
        warnings.append("存在未登记变量：" + "、".join(dict.fromkeys(undeclared)))
    return warnings


def _parse_block(title: str, lines: list[str], default_scenario: str) -> dict[str, object]:
    values: dict[str, object] = {"title": title, "scenario": default_scenario, "content": ""}
    content_lines: list[str] = []
    for line in lines:
        match = re.match(r"^[【\[]([^】\]]+)[】\]]\s*[:：]?\s*(.*)$", line.strip())
        if match and match.group(1).strip() in _META:
            key = _META[match.group(1).strip()]
            value = match.group(2).strip()
            if key in {"required_fields", "forbidden_phrases"}:
                values[key] = [
                    part.strip() for part in re.split(r"[,，、;；]", value) if part.strip()
                ]
            else:
                values[key] = value
            continue
        if line.strip() in {"【直接回复版】", "【更简短版】", "【稍微亲和版】"}:
            content_lines.append(line.strip())
        elif line.strip():
            content_lines.append(line.strip())
    values["content"] = "\n".join(content_lines).strip()
    return values


def parse_scripts(
    filename: str, data: bytes, default_scenario: str = "未分类"
) -> list[dict[str, object]]:
    text = extract_text(filename, data)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[tuple[str, list[str]]] = []
    current_title = Path(filename).stem
    current_lines: list[str] = []
    for line in lines:
        heading = _HEADING.match(line)
        if heading and current_lines:
            blocks.append((current_title, current_lines))
            current_title, current_lines = heading.group(1).strip(), []
        elif heading:
            current_title = heading.group(1).strip()
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append((current_title, current_lines))
    parsed = [_parse_block(title, block, default_scenario) for title, block in blocks]
    return [item for item in parsed if str(item.get("content", "")).strip()]


def parse_xlsx_cases(data: bytes) -> list[dict[str, object]]:
    """读取客户问答 Excel；只要求两列，不把业务字段负担交给员工。"""
    rows = _xlsx_rows(data)
    if not rows:
        return []
    headers = [cell.strip() for cell in rows[0]]
    question_index = next(
        (index for index, value in enumerate(headers) if "客户" in value and "询问" in value),
        None,
    )
    answer_index = next(
        (index for index, value in enumerate(headers) if "员工" in value and "回复" in value),
        None,
    )
    if question_index is None or answer_index is None:
        raise ValueError("Excel 必须包含两列：客户当时询问的什么、员工实际回复")
    cases = []
    if len(rows) > MAX_IMPORT_CASES + 1:
        raise ImportLimitError(f"Excel 每次最多支持 {MAX_IMPORT_CASES} 行话术")
    for row_number, row in enumerate(rows[1:], start=2):
        question = row[question_index].strip() if question_index < len(row) else ""
        answer = row[answer_index].strip() if answer_index < len(row) else ""
        if question and answer:
            cases.append(
                {
                    "title": f"客户问答案例第{row_number}行",
                    "customer_question": question,
                    "employee_reply": answer,
                    "content": answer,
                    "source_row": row_number,
                }
            )
    return cases
