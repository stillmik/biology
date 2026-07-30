import io
import re
from dataclasses import dataclass

import pdfplumber
from fastapi import HTTPException

from ..core.config import MAX_DOCUMENT_EXTRACTED_TOKENS, MAX_DOCUMENT_FILE_BYTES
from ..utils.chat_context import estimate_tokens
from .file_extraction_service import convert_pdf_table_to_markdown, normalize_extracted_text


@dataclass(frozen=True)
class ExtractedDocumentTable:
    table_number: int
    rows: list[list[str]]
    markdown: str
    token_count: int


@dataclass(frozen=True)
class ExtractedDocumentPage:
    page_number: int
    narrative_text: str
    tables: list[ExtractedDocumentTable]
    headings: list[str]
    token_count: int
    extraction_warnings: list[str]


@dataclass(frozen=True)
class ExtractedPdfDocument:
    pages: list[ExtractedDocumentPage]
    token_count: int


def validate_pdf_upload(filename: str, content_type: str | None, file_bytes: bytes) -> None:
    normalized_filename = filename.lower()
    allowed_content_types = {None, "", "application/pdf", "application/octet-stream"}

    if not normalized_filename.endswith(".pdf") or content_type not in allowed_content_types:
        raise HTTPException(status_code=415, detail="The document library accepts text-based PDF files only")

    if len(file_bytes) > MAX_DOCUMENT_FILE_BYTES:
        maximum_megabytes = MAX_DOCUMENT_FILE_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"PDF file cannot exceed {maximum_megabytes} MB")


def normalize_table_rows(raw_table: list[list[str | None]]) -> list[list[str]]:
    normalized_rows: list[list[str]] = []

    for raw_row in raw_table:
        normalized_row: list[str] = []

        for raw_cell in raw_row:
            normalized_cell = normalize_extracted_text(raw_cell or "")
            normalized_row.append(normalized_cell)

        if any(normalized_row):
            normalized_rows.append(normalized_row)

    return normalized_rows


def object_is_inside_table(object_data: dict, table_bounding_boxes: list[tuple[float, float, float, float]]) -> bool:
    object_center_x = (float(object_data["x0"]) + float(object_data["x1"])) / 2
    object_center_y = (float(object_data["top"]) + float(object_data["bottom"])) / 2

    for left, top, right, bottom in table_bounding_boxes:
        horizontal_match = left <= object_center_x <= right
        vertical_match = top <= object_center_y <= bottom

        if horizontal_match and vertical_match:
            return True

    return False


def extract_narrative_without_tables(page, table_bounding_boxes: list[tuple[float, float, float, float]]) -> str:
    if not table_bounding_boxes:
        return normalize_extracted_text(page.extract_text() or "")

    filtered_page = page.filter(
        lambda object_data: not object_is_inside_table(object_data, table_bounding_boxes)
    )
    return normalize_extracted_text(filtered_page.extract_text() or "")


def detect_heading_candidates(narrative_text: str) -> list[str]:
    headings: list[str] = []

    for raw_line in narrative_text.splitlines():
        candidate = raw_line.strip()

        if not candidate or len(candidate) > 120:
            continue

        word_count = len(candidate.split())
        uppercase_ratio = sum(character.isupper() for character in candidate) / max(1, len(candidate))
        resembles_numbered_heading = bool(re.match(r"^(\d+(\.\d+)*|[IVX]+)[.)]?\s+\S", candidate))
        resembles_short_title = word_count <= 12 and uppercase_ratio >= 0.35

        if resembles_numbered_heading or resembles_short_title:
            headings.append(candidate)

    return headings[:20]


def extract_page_tables(page) -> tuple[list[ExtractedDocumentTable], list[tuple[float, float, float, float]]]:
    extracted_tables: list[ExtractedDocumentTable] = []
    table_bounding_boxes: list[tuple[float, float, float, float]] = []

    for table_number, located_table in enumerate(page.find_tables(), start=1):
        normalized_rows = normalize_table_rows(located_table.extract())

        if not normalized_rows:
            continue

        markdown = convert_pdf_table_to_markdown(normalized_rows)
        extracted_table = ExtractedDocumentTable(
            table_number=table_number,
            rows=normalized_rows,
            markdown=markdown,
            token_count=estimate_tokens(markdown),
        )
        extracted_tables.append(extracted_table)
        table_bounding_boxes.append(tuple(located_table.bbox))

    return extracted_tables, table_bounding_boxes


def extract_structured_pdf(file_bytes: bytes) -> ExtractedPdfDocument:
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The PDF file is empty")

    extracted_pages: list[ExtractedDocumentPage] = []

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                tables, table_bounding_boxes = extract_page_tables(page)
                narrative_text = extract_narrative_without_tables(page, table_bounding_boxes)
                headings = detect_heading_candidates(narrative_text)
                warnings: list[str] = []

                if not narrative_text and not tables:
                    warnings.append("No extractable text or tables were found on this page")

                table_tokens = sum(table.token_count for table in tables)
                narrative_tokens = estimate_tokens(narrative_text) if narrative_text else 0
                page_token_count = narrative_tokens + table_tokens
                extracted_page = ExtractedDocumentPage(
                    page_number=page_number,
                    narrative_text=narrative_text,
                    tables=tables,
                    headings=headings,
                    token_count=page_token_count,
                    extraction_warnings=warnings,
                )
                extracted_pages.append(extracted_page)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail="Could not extract readable text or tables from this PDF",
        ) from error

    extracted_token_count = sum(page.token_count for page in extracted_pages)

    if extracted_token_count == 0:
        raise HTTPException(status_code=422, detail="The PDF contains no extractable text or tables")

    if extracted_token_count > MAX_DOCUMENT_EXTRACTED_TOKENS:
        detail = f"Extracted PDF content cannot exceed approximately {MAX_DOCUMENT_EXTRACTED_TOKENS} tokens"
        raise HTTPException(status_code=413, detail=detail)

    return ExtractedPdfDocument(pages=extracted_pages, token_count=extracted_token_count)
