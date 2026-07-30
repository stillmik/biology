import json
import logging

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.errors import UniqueViolation

from ..core.observability import log_event
from ..schemas.chat import ChatRequest, ChatResponse, ContextBudgetError
from ..services.chat_service import generate_chat_reply
from ..services.file_extraction_service import create_message_with_uploaded_file
from ..services.document_chat_service import (
    get_active_documents_for_chat,
    stream_queued_document_question_events,
)
from ..services.document_library_service import create_library_document
from ..services.file_extraction_service import get_file_extension
from ..infrastructure.document_repository import create_queued_document_question_from_db
from ..infrastructure.document_repository import enqueue_document_retry_from_db
from ..services.streaming_service import stream_chat_events
from ..utils.chat_context import validate_user_and_conversation


router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)
STREAM_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@router.post("", response_model=ChatResponse)
def create_chat_response(request: ChatRequest) -> ChatResponse:
    validate_user_and_conversation(request.user_id, request.conversation_id)
    active_documents = get_active_documents_for_chat(request)

    if active_documents:
        raise HTTPException(
            status_code=409,
            detail="Document-grounded conversations use the streaming endpoint so queued analysis can be reported.",
        )

    try:
        return ChatResponse(reply=generate_chat_reply(request.user_id, request.conversation_id, request.message))
    except ContextBudgetError as error:
        log_event(logger, logging.WARNING, "context_budget_exceeded", conversation_id=request.conversation_id)
        raise HTTPException(status_code=413, detail=str(error)) from error
    except Exception as error:
        log_event(logger, logging.ERROR, "chat_generation_failed", conversation_id=request.conversation_id, exception_type=type(error).__name__)
        raise HTTPException(status_code=502, detail="The model provider request failed") from error


@router.post("/stream")
def stream_chat_response(request: ChatRequest) -> StreamingResponse:
    validate_user_and_conversation(request.user_id, request.conversation_id)
    active_documents = get_active_documents_for_chat(request)

    if active_documents:
        return StreamingResponse(
            stream_queued_document_question_events(request, active_documents),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )

    return StreamingResponse(stream_chat_events(request), media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("/stream-with-file")
async def stream_chat_response_with_file(user_id: Annotated[int, Form(gt=0)], conversation_id: Annotated[int, Form(gt=0)], message: Annotated[str, Form()], file: Annotated[UploadFile, File()], generate_file: Annotated[bool, Form()] = False) -> StreamingResponse:
    request = ChatRequest(user_id=user_id, conversation_id=conversation_id, message=message, generate_file=generate_file)
    validate_user_and_conversation(request.user_id, request.conversation_id)

    filename = (file.filename or "attachment").strip()

    if get_file_extension(filename) == ".pdf":
        file_bytes = await file.read()
        document, reused = create_library_document(
            user_id,
            filename,
            file.content_type,
            file_bytes,
        )

        if document["status"] in {"failed", "cancelled"}:
            enqueue_document_retry_from_db(document["id"], user_id)
            document["status"] = "queued"
            document["progress_percent"] = 0
        message_content = (
            f"{request.message}\n\n"
            f"[Attached PDF: {document['filename']} | document:{document['id']}]"
        )
        try:
            user_message, answer_job = create_queued_document_question_from_db(
                user_id,
                conversation_id,
                request.message,
                [document["id"]],
                attach_document_ids=[document["id"]],
                message_content=message_content,
            )
        except UniqueViolation as error:
            raise HTTPException(
                status_code=409,
                detail="Wait for the current document answer to finish before asking another question.",
            ) from error

        def stream_upload_events():
            document_payload = {
                "id": document["id"],
                "filename": document["filename"],
                "status": document["status"],
                "progress_percent": document["progress_percent"],
                "reused": reused,
            }
            yield "data: " + json.dumps({"document": document_payload}) + "\n\n"
            yield "data: " + json.dumps(
                {
                    "answer_job": {
                        "id": answer_job["id"],
                        "status": answer_job["status"],
                        "user_message_id": user_message["id"],
                    }
                }
            ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_upload_events(),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )

    request.message = await create_message_with_uploaded_file(request.message, file)
    return StreamingResponse(stream_chat_events(request), media_type="text/event-stream", headers=STREAM_HEADERS)
