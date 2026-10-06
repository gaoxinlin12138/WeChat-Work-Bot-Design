import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader


class UnsupportedDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    content: str
    page_number: int | None = None
    printed_page: str | None = None
    element_type: str = "text"
    section_path: tuple[str, ...] = ()
    clause_numbers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


SUPPORTED_EXTENSIONS = {".md", ".pdf", ".txt"}
_CLAUSE_PATTERN = re.compile(r"(?<![\d.])([1-9]\d?(?:\.\d+){1,4})(?![\d.])")
_PRINTED_PAGE_PATTERN = re.compile(r"页码\s*[：:]\s*(\d+)\s*/\s*(\d+)")
_HEADER_TOKENS = ("文件编号", "文件名称", "版次", "页码")
_ADMIN_TABLE_TOKENS = ("修订记录", "修订日期", "会签记录", "会签部门")


def extract_pages(path: Path) -> list[ExtractedPage]:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocumentError(
            f"暂不支持 {extension or '无扩展名'} 文件，仅支持 TXT、Markdown 和 PDF"
        )
    if extension != ".pdf":
        return [ExtractedPage(path.read_text(encoding="utf-8-sig").strip())]

    reader = PdfReader(path)
    body_pages: list[ExtractedPage] = []
    for page_number, page in enumerate(reader.pages, start=1):
        content = (page.extract_text() or "").strip()
        image_count = _page_image_count(page)
        metadata = _page_quality_metadata(content, image_count)
        body_pages.append(
            ExtractedPage(
                content=content,
                page_number=page_number,
                printed_page=_printed_page(content),
                element_type="text",
                metadata=metadata,
            )
        )

    body_pages = _strip_repeated_marginal_lines(body_pages)
    tables_by_page = _extract_tables(path)

    extracted: list[ExtractedPage] = []
    for page in body_pages:
        extracted.append(page)
        page_number = page.page_number or 0
        for table_index, table in enumerate(tables_by_page.get(page_number, []), start=1):
            content, table_metadata = table
            extracted.append(
                ExtractedPage(
                    content=content,
                    page_number=page.page_number,
                    printed_page=page.printed_page,
                    element_type="table",
                    metadata={"table_index": table_index, **table_metadata},
                )
            )
    return extracted


def summarize_extraction(pages: list[ExtractedPage]) -> dict[str, Any]:
    body_pages = [page for page in pages if page.element_type == "text"]
    needs_ocr_pages = [
        page.page_number for page in body_pages if page.metadata.get("needs_ocr")
    ]
    low_quality_pages = [
        page.page_number for page in body_pages if page.metadata.get("low_quality")
    ]
    image_pages = [
        page.page_number for page in body_pages if int(page.metadata.get("image_count", 0)) > 0
    ]
    table_pages = sorted(
        {page.page_number for page in pages if page.element_type == "table" and page.page_number}
    )
    if needs_ocr_pages and len(needs_ocr_pages) == len(body_pages):
        document_type = "scanned_pdf"
    elif needs_ocr_pages or image_pages:
        document_type = "mixed_pdf"
    else:
        document_type = "native_text_pdf"
    summary: dict[str, Any] = {
        "document_type": document_type,
        "page_count": len(body_pages),
        "needs_ocr_pages": needs_ocr_pages,
        "low_quality_pages": low_quality_pages,
        "image_pages": image_pages,
        "table_pages": table_pages,
        "table_count": sum(page.element_type == "table" for page in pages),
    }
    return summary



def chunk_pages(
    pages: list[ExtractedPage],
    *,
    max_chars: int = 800,
    overlap_chars: int = 100,
) -> list[ExtractedPage]:
    """Split pages on clauses and retain provenance needed for reliable citations."""
    if max_chars <= overlap_chars:
        raise ValueError("max_chars 必须大于 overlap_chars")
    chunks: list[ExtractedPage] = []
    context_path: list[tuple[int, str]] = []
    for page in pages:
        text = "\n".join(line.strip() for line in page.content.splitlines() if line.strip())
        if not text:
            continue
        if page.element_type == "table":
            chunks.extend(
                _split_plain_text(
                    text,
                    page,
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                    section_path=tuple(item[1] for item in context_path),
                )
            )
            continue

        lines = text.splitlines()
        blocks: list[tuple[int, str, tuple[str, ...]]] = []
        current: list[str] = []
        current_level = 99
        current_block_path: tuple[str, ...] = tuple(item[1] for item in context_path)
        path: list[tuple[int, str]] = context_path.copy()
        saw_heading = False

        for line in lines:
            heading = _heading_info(line)
            if heading:
                saw_heading = True
                if current:
                    blocks.append(
                        (current_level, "\n".join(current).strip(), current_block_path)
                    )
                    current = []
                level, title = heading
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((level, title))
                current_level = level
                current_block_path = tuple(item[1] for item in path)
                current = [" / ".join(current_block_path)]
            elif current:
                current.append(line)
            else:
                current_level = 99
                current_block_path = tuple(item[1] for item in path)
                current = (
                    [" / ".join(current_block_path), line]
                    if current_block_path
                    else [line]
                )
        if current:
            blocks.append((current_level, "\n".join(current).strip(), current_block_path))

        if not saw_heading:
            chunks.extend(
                _split_plain_text(
                    text,
                    page,
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                    section_path=tuple(item[1] for item in context_path),
                )
            )
            continue

        context_path = path
        pending = ""
        pending_level = 99
        pending_path: tuple[str, ...] = ()
        min_keep = max(32, int(max_chars * 0.45))
        for level, block, block_path in blocks:
            if len(block) > max_chars:
                if pending:
                    chunks.append(_chunk_from(page, pending, pending_path))
                    pending = ""
                chunks.extend(
                    _split_structured_block(
                        block,
                        page,
                        block_path,
                        max_chars,
                        overlap_chars,
                    )
                )
                continue
            if not pending:
                pending, pending_level, pending_path = block, level, block_path
                continue
            same_or_higher = level <= pending_level and len(pending) >= min_keep
            if same_or_higher or len(pending) + 1 + len(block) > max_chars:
                chunks.append(_chunk_from(page, pending, pending_path))
                pending, pending_level, pending_path = block, level, block_path
            else:
                previous_content = pending
                pending = f"{pending}\n{block}"
                pending_level = max(pending_level, level)
                if (
                    pending_path
                    and block_path[: len(pending_path)] == pending_path
                    and previous_content == " / ".join(pending_path)
                ):
                    pending_path = block_path
                else:
                    pending_path = _common_path(pending_path, block_path)
        if pending:
            chunks.append(_chunk_from(page, pending, pending_path))
    return chunks


def _heading_info(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    candidate = stripped.lstrip("#").strip()
    if not candidate:
        return None
    decimal = re.match(r"^(\d+(?:\.\d+){1,4})(?:[、.：:]\s*|\s+)\S+", candidate)
    if decimal:
        return min(3 + decimal.group(1).count("."), 6), candidate
    patterns = (
        (1, r"^第[0-9一二三四五六七八九十百千万]+章(?:\s+|[:：])?.+"),
        (2, r"^第[0-9一二三四五六七八九十百千万]+节(?:\s+|[:：])?.+"),
        (3, r"^[一二三四五六七八九十百千万]+、\s*\S+"),
        (4, r"^（[一二三四五六七八九十百千万0-9]+）\s*\S+"),
        (3, r"^\d{1,2}(?:[、：:]\s*|\s+)\S+"),
    )
    for level, pattern in patterns:
        if re.match(pattern, candidate):
            return level, candidate
    if stripped.startswith("#"):
        return min(len(stripped) - len(stripped.lstrip("#")), 6), candidate
    return None


def _split_plain_text(
    text: str,
    page: ExtractedPage,
    *,
    max_chars: int,
    overlap_chars: int,
    section_path: tuple[str, ...],
) -> list[ExtractedPage]:
    step = max_chars - overlap_chars
    result: list[ExtractedPage] = []
    for start in range(0, len(text), step):
        content = text[start : start + max_chars].strip()
        if content:
            result.append(_chunk_from(page, content, section_path))
        if start + max_chars >= len(text):
            break
    return result


def _split_structured_block(
    block: str,
    page: ExtractedPage,
    section_path: tuple[str, ...],
    max_chars: int,
    overlap_chars: int,
) -> list[ExtractedPage]:
    lines = block.splitlines()
    prefix = lines[0]
    body = "\n".join(lines[1:]).strip()
    if not body:
        return [_chunk_from(page, prefix[:max_chars], section_path)]
    available = max_chars - len(prefix) - 1
    if available < 80:
        available = max_chars
        body = block
        prefix = ""
    step = max(1, available - overlap_chars)
    result: list[ExtractedPage] = []
    for start in range(0, len(body), step):
        part = body[start : start + available].strip()
        if not part:
            continue
        content = f"{prefix}\n{part}".strip() if prefix else part
        result.append(_chunk_from(page, content, section_path))
        if start + available >= len(body):
            break
    return result


def _chunk_from(
    page: ExtractedPage, content: str, section_path: tuple[str, ...]
) -> ExtractedPage:
    clauses = tuple(dict.fromkeys(_CLAUSE_PATTERN.findall(content)))
    return replace(
        page,
        content=content,
        section_path=section_path,
        clause_numbers=clauses,
    )


def _common_path(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    length = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        length += 1
    return first[:length]


def _printed_page(text: str) -> str | None:
    match = _PRINTED_PAGE_PATTERN.search(text)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _page_quality_metadata(text: str, image_count: int) -> dict[str, Any]:
    compact = re.sub(r"\s+", "", text)
    replacement_ratio = compact.count("\ufffd") / max(1, len(compact))
    needs_ocr = len(compact) < 30 and image_count > 0
    low_quality = needs_ocr or replacement_ratio > 0.03 or not compact
    if needs_ocr:
        strategy = "ocr_required"
    elif image_count:
        strategy = "text_with_visuals"
    else:
        strategy = "native_text"
    return {
        "parse_strategy": strategy,
        "text_chars": len(compact),
        "image_count": image_count,
        "replacement_ratio": round(replacement_ratio, 4),
        "needs_ocr": needs_ocr,
        "low_quality": low_quality,
    }


def _page_image_count(page: Any) -> int:
    try:
        resources = page.get("/Resources")
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        xobjects = resources.get("/XObject") if resources else None
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        count = 0
        for value in (xobjects or {}).values():
            obj = value.get_object() if hasattr(value, "get_object") else value
            if obj.get("/Subtype") == "/Image":
                count += 1
        return count
    except Exception:
        return 0


def _margin_key(line: str) -> str:
    value = _PRINTED_PAGE_PATTERN.sub("页码", line)
    return re.sub(r"\s+", "", value).strip("-—_ ")


def _strip_repeated_marginal_lines(pages: list[ExtractedPage]) -> list[ExtractedPage]:
    candidates: Counter[str] = Counter()
    page_lines: list[list[str]] = []
    for page in pages:
        lines = [line.strip() for line in page.content.splitlines() if line.strip()]
        page_lines.append(lines)
        for line in [*lines[:4], *lines[-3:]]:
            key = _margin_key(line)
            if len(key) >= 6:
                candidates[key] += 1
    repeated = {key for key, count in candidates.items() if count >= 2}
    cleaned: list[ExtractedPage] = []
    for page, lines in zip(pages, page_lines, strict=True):
        if page.page_number == 1:
            cleaned.append(page)
            continue
        result: list[str] = []
        for index, line in enumerate(lines):
            marginal = index < 4 or index >= max(0, len(lines) - 3)
            explicit_header = sum(token in line for token in _HEADER_TOKENS) >= 2
            company_header = "Wenzhou Yuyin Sanitary Ware" in line and "文件编号" in line
            if marginal and (_margin_key(line) in repeated or explicit_header or company_header):
                continue
            result.append(line)
        cleaned.append(replace(page, content="\n".join(result)))
    return cleaned


def _extract_tables(path: Path) -> dict[int, list[tuple[str, dict[str, Any]]]]:
    result: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_tables: list[tuple[str, dict[str, Any]]] = []
            for table in page.find_tables():
                rows = [[_clean_cell(cell) for cell in row] for row in table.extract()]
                if not _is_business_table(rows):
                    continue
                content = _format_table(rows, page_number)
                bbox = [round(float(value), 2) for value in table.bbox]
                page_tables.append(
                    (
                        content,
                        {
                            "rows": len(rows),
                            "columns": max((len(row) for row in rows), default=0),
                            "bbox": bbox,
                        },
                    )
                )
            if page_tables:
                result[page_number] = page_tables
    return result


def _clean_cell(value: str | None) -> str:
    return " ".join((value or "").replace("\n", " ").split())


def _is_business_table(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or max((len(row) for row in rows), default=0) < 2:
        return False
    cells = [cell for row in rows for cell in row if cell]
    if len(cells) < 4 or sum(len(cell) for cell in cells) < 20:
        return False
    joined = " ".join(cells)
    if any(token in joined for token in _ADMIN_TABLE_TOKENS):
        return False
    if sum(token in joined for token in _HEADER_TOKENS) >= 3:
        return False
    return True


def _format_table(rows: list[list[str]], page_number: int) -> str:
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    usable_header = sum(bool(cell) for cell in header) >= 2
    labels = (
        [cell or f"列{index}" for index, cell in enumerate(header, start=1)]
        if usable_header
        else [f"列{index}" for index in range(1, width + 1)]
    )
    data_rows = normalized[1:] if usable_header else normalized
    lines = [f"表格（PDF第{page_number}页）"]
    for row_index, row in enumerate(data_rows, start=1):
        values = [f"{label}={value}" for label, value in zip(labels, row, strict=True) if value]
        if values:
            lines.append(f"第{row_index}行：" + "；".join(values))
    return "\n".join(lines)
