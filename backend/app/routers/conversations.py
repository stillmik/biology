import logging

from fastapi import APIRouter, HTTPException, Query

from ..core.observability import hash_identifier, log_event, observe_operation
from ..infrastructure.database import create_conversation_from_db, delete_conversation_from_db, get_conversation_from_db, get_user_from_db, list_conversation_messages_page_from_db, list_user_conversations_from_db, update_conversation_title_from_db
from ..schemas.chat import ConversationRequest, ConversationResponse, HistoryMessage
from ..utils.chat_context import create_conversation_response

router = APIRouter(prefix="/api", tags=["conversations"])
logger = logging.getLogger(__name__)


@router.get("/users/{user_id}/conversations", response_model=list[ConversationResponse])
def list_user_conversations(user_id: int) -> list[ConversationResponse]:
    with observe_operation("list_conversations"):
        if not get_user_from_db(user_id):
            raise HTTPException(status_code=404, detail="User not found")

        result = [create_conversation_response(conversation) for conversation in list_user_conversations_from_db(user_id)]
    return result


@router.post("/conversations", response_model=ConversationResponse)
def create_conversation(request: ConversationRequest) -> ConversationResponse:
    with observe_operation("create_conversation"):
        if not get_user_from_db(request.user_id):
            raise HTTPException(status_code=404, detail="User not found")

        conversation = create_conversation_from_db(request.user_id, request.title)
    log_event(logger, logging.INFO, "conversation_created", conversation_id=conversation["id"], user_id_hash=hash_identifier(request.user_id))
    return create_conversation_response(conversation)


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
def update_conversation(conversation_id: int, request: ConversationRequest) -> ConversationResponse:
    with observe_operation("rename_conversation"):
        conversation = update_conversation_title_from_db(conversation_id, request.user_id, request.title)

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
    log_event(logger, logging.INFO, "conversation_renamed", conversation_id=conversation_id, user_id_hash=hash_identifier(request.user_id))
    return create_conversation_response(conversation)


@router.get("/conversations/{conversation_id}/messages", response_model=list[HistoryMessage])
def list_conversation_messages(conversation_id: int, user_id: int, limit: int = Query(default=50, ge=1, le=200), before_id: int | None = Query(default=None, gt=0)) -> list[HistoryMessage]:
    with observe_operation("load_conversation_history"):
        if not get_conversation_from_db(conversation_id, user_id):
            raise HTTPException(status_code=404, detail="Conversation not found")

        rows = list_conversation_messages_page_from_db(conversation_id=conversation_id, limit=limit, before_id=before_id)
        result = [HistoryMessage(id=row["id"], role=row["role"], content=row["content"], created_at=row["created_at"].isoformat(), generated_file={"id": row["generated_file_id"], "filename": row["generated_file_name"], "mime_type": row["generated_file_mime_type"]} if row["generated_file_id"] else None) for row in rows]
    return result


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, user_id: int) -> dict[str, bool]:
    with observe_operation("delete_conversation"):
        if not delete_conversation_from_db(conversation_id, user_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
    log_event(logger, logging.INFO, "conversation_deleted", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id))
    return {"deleted": True}
