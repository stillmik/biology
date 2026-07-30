import hashlib
import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..core.config import DOCUMENT_ANALYSIS_VERSION, DOCUMENT_STORAGE_DIRECTORY
from ..infrastructure.document_repository import create_or_get_document_from_db
from .document_extraction_service import validate_pdf_upload


def sanitize_document_filename(filename: str) -> str:
    basename = Path(filename).name.strip()
    sanitized = re.sub(r"[^A-Za-z0-9._ -]+", "_", basename)
    return sanitized[:240] or "document.pdf"


def ensure_document_storage_directory() -> Path:
    storage_directory = Path(DOCUMENT_STORAGE_DIRECTORY).resolve()
    storage_directory.mkdir(parents=True, exist_ok=True)
    return storage_directory


def write_document_file_atomically(storage_name: str, file_bytes: bytes) -> Path:
    storage_directory = ensure_document_storage_directory()
    final_path = (storage_directory / storage_name).resolve()

    if storage_directory not in final_path.parents:
        raise RuntimeError("Invalid document storage target")

    temporary_path = final_path.with_suffix(final_path.suffix + ".uploading")
    temporary_path.write_bytes(file_bytes)
    os.replace(temporary_path, final_path)
    return final_path


def create_library_document(
    user_id: int,
    filename: str,
    content_type: str | None,
    file_bytes: bytes,
) -> tuple[dict, bool]:
    validate_pdf_upload(filename, content_type, file_bytes)
    document_id = str(uuid.uuid4())
    safe_filename = sanitize_document_filename(filename)
    storage_name = f"{document_id}.pdf"
    checksum_sha256 = hashlib.sha256(file_bytes).hexdigest()
    storage_path = write_document_file_atomically(storage_name, file_bytes)

    try:
        document, created = create_or_get_document_from_db(
            document_id=document_id,
            user_id=user_id,
            filename=safe_filename,
            storage_name=storage_name,
            checksum_sha256=checksum_sha256,
            analysis_version=DOCUMENT_ANALYSIS_VERSION,
        )
    except Exception:
        storage_path.unlink(missing_ok=True)
        raise

    if not created:
        storage_path.unlink(missing_ok=True)

    return document, not created


def remove_document_file(storage_name: str) -> None:
    storage_directory = ensure_document_storage_directory()
    storage_path = (storage_directory / storage_name).resolve()

    if storage_directory not in storage_path.parents:
        raise HTTPException(status_code=409, detail="Document storage record is invalid")

    storage_path.unlink(missing_ok=True)


def get_document_storage_path(storage_name: str) -> Path:
    storage_directory = ensure_document_storage_directory()
    storage_path = (storage_directory / storage_name).resolve()

    if storage_directory not in storage_path.parents:
        raise HTTPException(status_code=404, detail="Document file not found")

    if not storage_path.is_file():
        raise HTTPException(status_code=404, detail="Document file not found")

    return storage_path
