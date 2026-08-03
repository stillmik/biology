import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from dataclasses import replace
from statistics import median

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
        extracted_text = page.extract_text() or ""
        return normalize_extracted_text(extracted_text)

    filtered_page = page.filter(lambda object_data: not object_is_inside_table(object_data, table_bounding_boxes))
    extracted_text = filtered_page.extract_text() or ""
    return normalize_extracted_text(extracted_text)


def normalize_heading_for_comparison(heading: str) -> str:
    return re.sub(r"\s+", " ", heading.casefold()).strip()


def is_heading_line(line_text: str, line_words: list[dict], body_font_size: float) -> bool:
    if not line_text or len(line_text) > 120:
        return False

    word_count = len(line_text.split())

    if word_count > 14:
        return False

    excluded_patterns = (r"^page\s+\d+", r"^reference id:", r"^nda#?:", r"^\(?[a-z]\)?\s*\(\d+\)")

    if any(re.match(pattern, line_text, re.IGNORECASE) for pattern in excluded_patterns):
        return False

    has_table_of_contents_leader = bool(re.search(r"[.…·]{3,}", line_text))

    if has_table_of_contents_leader or line_text.endswith((".", ",", ";")):
        return False

    line_font_sizes = [float(word.get("size", body_font_size)) for word in line_words]
    typical_line_font_size = median(line_font_sizes)
    bold_word_count = sum("bold" in str(word.get("fontname", "")).casefold() for word in line_words)
    uppercase_characters = sum(character.isupper() for character in line_text)
    alphabetic_characters = sum(character.isalpha() for character in line_text)
    uppercase_ratio = uppercase_characters / max(1, alphabetic_characters)
    has_prominent_size = typical_line_font_size >= body_font_size + 1
    is_mostly_bold = bold_word_count >= max(1, len(line_words) // 2)
    is_short_uppercase = word_count <= 12 and uppercase_ratio >= 0.7
    return has_prominent_size or is_mostly_bold or is_short_uppercase


def detect_heading_candidates(page, table_bounding_boxes: list[tuple[float, float, float, float]]) -> list[str]:
    words = page.extract_words(extra_attrs=["size", "fontname"])
    narrative_words = [word for word in words if not object_is_inside_table(word, table_bounding_boxes)]

    if not narrative_words:
        return []

    page_text = " ".join(str(word["text"]) for word in narrative_words).casefold()

    if "representation of an electronic record that was signed" in page_text:
        return []

    font_sizes = [float(word.get("size", 0)) for word in narrative_words if word.get("size")]
    body_font_size = median(font_sizes) if font_sizes else 0
    words_by_line: defaultdict[int, list[dict]] = defaultdict(list)

    for word in narrative_words:
        line_key = round(float(word["top"]) / 2)
        words_by_line[line_key].append(word)

    headings: list[str] = []

    for line_key in sorted(words_by_line):
        line_words = sorted(words_by_line[line_key], key=lambda word: float(word["x0"]))
        line_text = " ".join(str(word["text"]).strip() for word in line_words).strip()

        if is_heading_line(line_text, line_words, body_font_size):
            headings.append(line_text)

    return headings[:20]


def remove_repeated_heading_candidates(pages: list[ExtractedDocumentPage]) -> list[ExtractedDocumentPage]:
    normalized_headings = [normalize_heading_for_comparison(heading) for page in pages for heading in page.headings]
    heading_frequency = Counter(normalized_headings)
    cleaned_pages: list[ExtractedDocumentPage] = []

    for page in pages:
        unique_headings = [heading for heading in page.headings if heading_frequency[normalize_heading_for_comparison(heading)] < 3]
        cleaned_page = replace(page, headings=unique_headings)
        cleaned_pages.append(cleaned_page)

    return cleaned_pages


def extract_page_tables(page) -> tuple[list[ExtractedDocumentTable], list[tuple[float, float, float, float]]]:
    extracted_tables: list[ExtractedDocumentTable] = []
    table_bounding_boxes: list[tuple[float, float, float, float]] = []

    for table_number, located_table in enumerate(page.find_tables(), start=1):
        normalized_rows = normalize_table_rows(located_table.extract())

        if not normalized_rows:
            continue

        markdown = convert_pdf_table_to_markdown(normalized_rows)
        table_tokens = estimate_tokens(markdown)
        extracted_table = ExtractedDocumentTable(table_number=table_number, rows=normalized_rows, markdown=markdown, token_count=table_tokens)
        extracted_tables.append(extracted_table)
        table_bounding_boxes.append(tuple(located_table.bbox))

    return extracted_tables, table_bounding_boxes


def extract_pdf_page(page, page_number: int) -> ExtractedDocumentPage:
    tables, table_bounding_boxes = extract_page_tables(page)
    narrative_text = extract_narrative_without_tables(page, table_bounding_boxes)
    headings = detect_heading_candidates(page, table_bounding_boxes)
    extraction_warnings = [] if narrative_text or tables else ["No extractable text or tables were found on this page"]

    table_tokens = sum(table.token_count for table in tables)
    narrative_tokens = estimate_tokens(narrative_text) if narrative_text else 0

    return ExtractedDocumentPage(page_number=page_number, narrative_text=narrative_text, tables=tables, headings=headings, token_count=narrative_tokens + table_tokens, extraction_warnings=extraction_warnings)


def validate_extracted_document(pages: list[ExtractedDocumentPage]) -> int:
    extracted_token_count = sum(page.token_count for page in pages)

    if extracted_token_count == 0:
        raise HTTPException(status_code=422, detail="The PDF contains no extractable text or tables")

    if extracted_token_count > MAX_DOCUMENT_EXTRACTED_TOKENS:
        detail = f"Extracted PDF content cannot exceed approximately {MAX_DOCUMENT_EXTRACTED_TOKENS} tokens"
        raise HTTPException(status_code=413, detail=detail)

    return extracted_token_count


def extract_structured_pdf(file_bytes: bytes) -> ExtractedPdfDocument:
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The PDF file is empty")

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = [extract_pdf_page(page, page_number) for page_number, page in enumerate(pdf.pages, start=1)]
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail="Could not extract readable text or tables from this PDF") from error

    pages = remove_repeated_heading_candidates(pages)
    extracted_token_count = validate_extracted_document(pages)
    return ExtractedPdfDocument(pages=pages, token_count=extracted_token_count)
