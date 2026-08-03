import json
import logging

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.errors import UniqueViolation
from starlette.concurrency import run_in_threadpool


from ..core.observability import log_event
from ..schemas.chat import ChatRequest, ChatResponse, ContextBudgetError
from ..services.chat_service import generate_chat_reply
from ..services.document_chat_service import get_active_documents_for_chat, stream_queued_document_question_events
from ..services.document_library_service import document_to_s3_and_metainfo_to_db
from ..services.file_extraction_service import create_message_with_uploaded_file, get_file_extension
from ..infrastructure.document_job_repository import create_answer_job_and_attach_conversation_documents_in_db, enqueue_document_retry_from_db
from ..services.streaming_service import stream_chat_events
from ..utils.chat_context import validate_user_and_conversation

router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)
STREAM_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def stream_uploaded_document_events(document: dict, document_was_reused: bool, user_message: dict, answer_job: dict):
    document_status_payload = {"id": document["id"], "filename": document["filename"], "status": document["status"], "progress_percent": document["progress_percent"], "reused": document_was_reused}
    answer_job_status_payload = {"answer_job": {"id": answer_job["id"], "status": answer_job["status"], "user_message_id": user_message["id"]}}

    yield "data: " + json.dumps({"document": document_status_payload}) + "\n\n"
    yield "data: " + json.dumps(answer_job_status_payload) + "\n\n"
    yield "data: [DONE]\n\n"


def create_chat_request_from_data(user_id: int, conversation_id: int, message: str, generate_file: bool) -> ChatRequest:
    return ChatRequest(user_id=user_id, conversation_id=conversation_id, message=message, generate_file=generate_file)


def raise_if_generated_file_is_requested_for_document_answer(generate_file: bool) -> None:
    if generate_file:
        raise HTTPException(status_code=409, detail="Generated response files are not available for queued document answers")


async def store_uploaded_pdf_document_in_storage_and_db(user_id: int, uploaded_file: UploadFile) -> tuple[dict, bool]:
    uploaded_file_bytes = await uploaded_file.read()
    uploaded_document, document_was_reused = await run_in_threadpool(document_to_s3_and_metainfo_to_db, user_id, (uploaded_file.filename or "attachment").strip(), uploaded_file.content_type, uploaded_file_bytes)

    if uploaded_document["status"] in {"failed", "cancelled"}:
        await run_in_threadpool(enqueue_document_retry_from_db, uploaded_document["id"], user_id)
        uploaded_document["status"] = "queued"
        uploaded_document["progress_percent"] = 0

    return uploaded_document, document_was_reused


async def create_queued_pdf_answer_job_in_db(user_id: int, conversation_id: int, question: str, uploaded_document: dict) -> tuple[dict, dict]:
    attached_document_message = f"{question}\n\n[Attached PDF: {uploaded_document['filename']} | document:{uploaded_document['id']}]"
    try:
        user_message, answer_job = await run_in_threadpool(create_answer_job_and_attach_conversation_documents_in_db, user_id, conversation_id, question, [uploaded_document["id"]], [uploaded_document["id"]], attached_document_message)
    except UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Wait for the current document answer to finish before asking another question.") from error

    return user_message, answer_job


async def create_legacy_file_chat_stream(chat_request: ChatRequest, uploaded_file: UploadFile) -> StreamingResponse:
    chat_request.message = await create_message_with_uploaded_file(chat_request.message, uploaded_file)
    legacy_chat_events = stream_chat_events(chat_request)
    return StreamingResponse(legacy_chat_events, media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("", response_model=ChatResponse)
def create_chat_response(request: ChatRequest) -> ChatResponse:
    validate_user_and_conversation(request.user_id, request.conversation_id)
    active_documents = get_active_documents_for_chat(request)

    if active_documents:
        raise HTTPException(status_code=409, detail="Document-grounded conversations use the streaming endpoint so queued analysis can be reported.")

    try:
        generated_reply = generate_chat_reply(request.user_id, request.conversation_id, request.message)
        return ChatResponse(reply=generated_reply)
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
        if request.generate_file:
            raise HTTPException(status_code=409, detail="Generated response files are not available for queued document answers")

        document_question_events = stream_queued_document_question_events(request, active_documents)
        return StreamingResponse(document_question_events, media_type="text/event-stream", headers=STREAM_HEADERS)

    chat_events = stream_chat_events(request)
    return StreamingResponse(chat_events, media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("/stream-with-file")
async def stream_chat_response_with_file(user_id: Annotated[int, Form(gt=0)], conversation_id: Annotated[int, Form(gt=0)], message: Annotated[str, Form()], file: Annotated[UploadFile, File()], generate_file: Annotated[bool, Form()] = False) -> StreamingResponse:
    chat_request = create_chat_request_from_data(user_id, conversation_id, message, generate_file)
    await run_in_threadpool(validate_user_and_conversation, chat_request.user_id, chat_request.conversation_id)

    uploaded_filename = (file.filename or "attachment").strip()
    uploaded_file_extension = get_file_extension(uploaded_filename)

    if uploaded_file_extension == ".pdf":
        raise_if_generated_file_is_requested_for_document_answer(chat_request.generate_file)
        document, document_was_reused = await store_uploaded_pdf_document_in_storage_and_db(user_id, file)
        user_message, answer_job = await create_queued_pdf_answer_job_in_db(user_id, conversation_id, chat_request.message, document)
        upload_events = stream_uploaded_document_events(document, document_was_reused, user_message, answer_job)
        return StreamingResponse(upload_events, media_type="text/event-stream", headers=STREAM_HEADERS)

    return await create_legacy_file_chat_stream(chat_request, file)
