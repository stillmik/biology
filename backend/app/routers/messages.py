import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..core.observability import log_event
from ..infrastructure.database import get_user_message_from_db
from ..schemas.chat import ChatResponse, ContextBudgetError, MessageEditRequest
from ..services.chat_service import regenerate_chat_reply
from ..services.streaming_service import stream_regenerated_message_events


router = APIRouter(prefix="/api/messages", tags=["messages"])
logger = logging.getLogger(__name__)
STREAM_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def get_editable_user_message(message_id: int, user_id: int) -> dict:
    message = get_user_message_from_db(message_id, user_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message["role"] != "user":
        raise HTTPException(status_code=409, detail="Only user messages can be edited")
    return message


@router.patch("/{message_id}", response_model=ChatResponse)
def update_user_message(message_id: int, request: MessageEditRequest) -> ChatResponse:
    message = get_editable_user_message(message_id, request.user_id)
    try:
        reply = regenerate_chat_reply(message_id, request.user_id, request.content)
        if not reply:
            raise HTTPException(status_code=404, detail="Message not found")
        return ChatResponse(reply=reply)
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ContextBudgetError as error:
        log_event(logger, logging.WARNING, "context_budget_exceeded", conversation_id=message["conversation_id"])
        raise HTTPException(status_code=413, detail=str(error)) from error
    except Exception as error:
        log_event(logger, logging.ERROR, "message_regeneration_failed", conversation_id=message["conversation_id"], message_id=message_id, exception_type=type(error).__name__)
        raise HTTPException(status_code=502, detail="The model provider request failed") from error


@router.patch("/{message_id}/stream")
def stream_updated_user_message_response(message_id: int, request: MessageEditRequest) -> StreamingResponse:
    get_editable_user_message(message_id, request.user_id)
    return StreamingResponse(stream_regenerated_message_events(message_id, request.user_id, request.content), media_type="text/event-stream", headers=STREAM_HEADERS)
