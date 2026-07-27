import logging

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..core.observability import log_event
from ..schemas.chat import ChatRequest, ChatResponse, ContextBudgetError
from ..services.chat_service import generate_chat_reply
from ..services.file_extraction_service import create_message_with_uploaded_file
from ..services.streaming_service import stream_chat_events
from ..utils.chat_context import validate_user_and_conversation


router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)
STREAM_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@router.post("", response_model=ChatResponse)
def create_chat_response(request: ChatRequest) -> ChatResponse:
    validate_user_and_conversation(request.user_id, request.conversation_id)

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
    return StreamingResponse(stream_chat_events(request), media_type="text/event-stream", headers=STREAM_HEADERS)


@router.post("/stream-with-file")
async def stream_chat_response_with_file(user_id: Annotated[int, Form(gt=0)], conversation_id: Annotated[int, Form(gt=0)], message: Annotated[str, Form()], file: Annotated[UploadFile, File()], generate_file: Annotated[bool, Form()] = False) -> StreamingResponse:
    request = ChatRequest(user_id=user_id, conversation_id=conversation_id, message=message, generate_file=generate_file)
    request.message = await create_message_with_uploaded_file(request.message, file)
    validate_user_and_conversation(request.user_id, request.conversation_id)
    return StreamingResponse(stream_chat_events(request), media_type="text/event-stream", headers=STREAM_HEADERS)
