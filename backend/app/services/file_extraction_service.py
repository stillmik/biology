import io
import re

import pdfplumber

from fastapi import HTTPException, UploadFile

from ..core.config import MAX_ATTACHED_FILE_BYTES, MAX_ATTACHED_FILE_LENGTH
from ..utils.chat_context import truncate_to_tokens


SUPPORTED_FILE_EXTENSIONS = {".txt", ".pdf"}
SUPPORTED_CONTENT_TYPES = {"text/plain", "application/pdf"}


def get_file_extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def normalize_extracted_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value.replace("\x00", "")).strip()


def convert_pdf_table_to_markdown(table: list[list[str | None]]) -> str:
    rows = [[normalize_extracted_text(cell or "") for cell in row] for row in table if any(cell and cell.strip() for cell in row)]
    if not rows:
        return ""
    column_count = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
    header = normalized_rows[0]
    separator = ["---"] * column_count
    return "\n".join("| " + " | ".join(row) + " |" for row in [header, separator, *normalized_rows[1:]])


def extract_text_and_tables_from_pdf(file_bytes: bytes) -> str:
    extracted_parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_parts = [normalize_extracted_text(page.extract_text() or "")]
                page_parts.extend(convert_pdf_table_to_markdown(table) for table in page.extract_tables())
                content = normalize_extracted_text("\n\n".join(part for part in page_parts if part))
                if content:
                    extracted_parts.append(f"Page {page_number}:\n{content}")
    except Exception as error:
        raise HTTPException(status_code=422, detail="Could not extract readable text or tables from this PDF") from error
    return normalize_extracted_text("\n\n".join(extracted_parts))


def extract_text_from_uploaded_file(filename: str, content_type: str | None, file_bytes: bytes) -> str:
    extension = get_file_extension(filename)
    if extension not in SUPPORTED_FILE_EXTENSIONS or content_type not in SUPPORTED_CONTENT_TYPES | {None, "", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Only TXT and PDF files are supported")
    if len(file_bytes) > MAX_ATTACHED_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"Attached file cannot exceed {MAX_ATTACHED_FILE_BYTES // (1024 * 1024)} MB")
    if extension == ".txt":
        try:
            extracted_text = file_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=422, detail="TXT files must use UTF-8 encoding") from error
    else:
        extracted_text = extract_text_and_tables_from_pdf(file_bytes)
    extracted_text = normalize_extracted_text(extracted_text)
    if not extracted_text:
        raise HTTPException(status_code=422, detail="The attached file contains no readable text or tables")
    return truncate_to_tokens(extracted_text, MAX_ATTACHED_FILE_LENGTH)


async def create_message_with_uploaded_file(message: str, uploaded_file: UploadFile) -> str:
    filename = (uploaded_file.filename or "attachment").strip()
    extracted_text = extract_text_from_uploaded_file(filename, uploaded_file.content_type, await uploaded_file.read())
    return f"{message.strip()}\n\n[Attached file: {filename}]\n{extracted_text}"
