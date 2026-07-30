from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..infrastructure.document_repository import (
    attach_document_to_conversation_from_db,
    delete_document_for_user_from_db,
    detach_document_from_conversation_from_db,
    enqueue_document_retry_from_db,
    get_document_for_user_from_db,
    list_conversation_documents_from_db,
    list_documents_for_user_from_db,
    get_answer_job_for_user_from_db,
)
from ..infrastructure.database import get_user_from_db
from ..schemas.documents import (
    ConversationDocumentRequest,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    AnswerJobResponse,
)
from ..services.document_library_service import (
    create_library_document,
    get_document_storage_path,
    remove_document_file,
)
from ..utils.chat_context import validate_user_and_conversation


router = APIRouter(prefix="/api", tags=["documents"])


def create_document_response(document: dict) -> DocumentResponse:
    return DocumentResponse(
        id=document["id"],
        user_id=document["user_id"],
        filename=document["filename"],
        status=document["status"],
        analysis_mode=document["analysis_mode"],
        progress_percent=document["progress_percent"],
        page_count=document["page_count"],
        extracted_token_count=document["extracted_token_count"],
        summary=document["summary"],
        last_error=document["last_error"],
        created_at=document["created_at"].isoformat(),
        updated_at=document["updated_at"].isoformat(),
    )


@router.post("/documents", response_model=DocumentUploadResponse, status_code=202)
async def upload_document(
    user_id: Annotated[int, Form(gt=0)],
    file: Annotated[UploadFile, File()],
) -> DocumentUploadResponse:
    if not get_user_from_db(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    filename = (file.filename or "document.pdf").strip()
    file_bytes = await file.read()
    document, reused = create_library_document(
        user_id,
        filename,
        file.content_type,
        file_bytes,
    )
    return DocumentUploadResponse(
        document=create_document_response(document),
        reused=reused,
    )


@router.get("/users/{user_id}/documents", response_model=DocumentListResponse)
def list_user_documents(user_id: int) -> DocumentListResponse:
    if not get_user_from_db(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    documents = list_documents_for_user_from_db(user_id)
    responses = [create_document_response(document) for document in documents]
    return DocumentListResponse(documents=responses)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, user_id: Annotated[int, Query(gt=0)]) -> DocumentResponse:
    document = get_document_for_user_from_db(document_id, user_id)

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return create_document_response(document)


@router.get("/documents/{document_id}/file")
def download_document(
    document_id: str,
    user_id: Annotated[int, Query(gt=0)],
) -> FileResponse:
    document = get_document_for_user_from_db(document_id, user_id)

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    storage_path = get_document_storage_path(document["storage_name"])
    return FileResponse(
        path=storage_path,
        media_type="application/pdf",
        filename=document["filename"],
        content_disposition_type="inline",
    )


@router.post("/documents/{document_id}/retry", status_code=202)
def retry_document(
    document_id: str,
    request: ConversationDocumentRequest,
) -> dict:
    analysis_job = enqueue_document_retry_from_db(document_id, request.user_id)

    if not analysis_job:
        raise HTTPException(status_code=404, detail="Document not found")

    return {"analysis_job_id": analysis_job["id"], "status": analysis_job["status"]}


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    user_id: Annotated[int, Query(gt=0)],
) -> None:
    deleted_document = delete_document_for_user_from_db(document_id, user_id)

    if not deleted_document:
        raise HTTPException(status_code=404, detail="Document not found")

    remove_document_file(deleted_document["storage_name"])


@router.post(
    "/conversations/{conversation_id}/documents/{document_id}",
    status_code=204,
)
def attach_document(
    conversation_id: int,
    document_id: str,
    request: ConversationDocumentRequest,
) -> None:
    validate_user_and_conversation(request.user_id, conversation_id)
    attached = attach_document_to_conversation_from_db(
        conversation_id,
        document_id,
        request.user_id,
    )

    if not attached:
        raise HTTPException(status_code=404, detail="Document not found")


@router.delete(
    "/conversations/{conversation_id}/documents/{document_id}",
    status_code=204,
)
def detach_document(
    conversation_id: int,
    document_id: str,
    user_id: Annotated[int, Query(gt=0)],
) -> None:
    validate_user_and_conversation(user_id, conversation_id)
    detached = detach_document_from_conversation_from_db(
        conversation_id,
        document_id,
        user_id,
    )

    if not detached:
        raise HTTPException(status_code=404, detail="Attached document not found")


@router.get(
    "/conversations/{conversation_id}/documents",
    response_model=DocumentListResponse,
)
def list_conversation_documents(
    conversation_id: int,
    user_id: Annotated[int, Query(gt=0)],
) -> DocumentListResponse:
    validate_user_and_conversation(user_id, conversation_id)
    documents = list_conversation_documents_from_db(conversation_id, user_id)
    responses = [create_document_response(document) for document in documents]
    return DocumentListResponse(documents=responses)


@router.get("/answer-jobs/{answer_job_id}", response_model=AnswerJobResponse)
def get_answer_job(
    answer_job_id: int,
    user_id: Annotated[int, Query(gt=0)],
) -> AnswerJobResponse:
    answer_job = get_answer_job_for_user_from_db(answer_job_id, user_id)

    if not answer_job:
        raise HTTPException(status_code=404, detail="Answer job not found")

    return AnswerJobResponse(
        id=answer_job["id"],
        conversation_id=answer_job["conversation_id"],
        user_message_id=answer_job["user_message_id"],
        assistant_message_id=answer_job["assistant_message_id"],
        status=answer_job["status"],
        last_error=answer_job["last_error"],
        created_at=answer_job["created_at"].isoformat(),
        updated_at=answer_job["updated_at"].isoformat(),
    )
