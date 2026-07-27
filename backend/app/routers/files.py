from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..core.config import GENERATED_FILES_DIRECTORY
from ..infrastructure.database import get_generated_file_for_user_from_db


router = APIRouter(prefix="/api/files", tags=["generated files"])


@router.get("/{file_id}")
def download_generated_file(file_id: str, user_id: int) -> FileResponse:
    generated_file = get_generated_file_for_user_from_db(file_id, user_id)
    if not generated_file:
        raise HTTPException(status_code=404, detail="Generated file not found")
    file_path = Path(GENERATED_FILES_DIRECTORY) / generated_file["storage_name"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Generated file is unavailable")
    return FileResponse(file_path, media_type=generated_file["mime_type"], filename=generated_file["filename"])
