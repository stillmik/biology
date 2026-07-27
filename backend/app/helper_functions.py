import math


from .db import get_conversation_from_db, get_user_from_db
from fastapi import HTTPException


from .states import ChatState, ConversationResponse


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 3))


def estimate_message_tokens(message: dict[str, str]) -> int:
    return 4 + estimate_tokens(message["content"])


def estimate_context_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def truncate_to_tokens(text: str, token_limit: int) -> str:
    return text[: max(1, token_limit * 4)]


def create_initial_graph_state(conversation_id: int) -> ChatState:
    return {"conversation_id": conversation_id, "attached_summaries": [], "included_summary": {"is_included": False, "segment_count": 0, "total_token_count": 0, "segments": []}, "summary_cursor": 0, "unsummarized_messages": [], "raw_message_tokens": 0, "projected_tokens": 0, "tokens_until_summarization": 0, "summarization_trigger_progress": 0.0, "should_summarize": False, "can_summarize": False, "summary_passes": 0, "summary_decision": "", "summary_reason": "", "summarizable_message_count": 0, "summary_messages_processed": 0, "summary_token_reduction": 0, "history": [], "context_budget_result": "not_checked"}


def create_conversation_response(conversation: dict) -> ConversationResponse:
    return ConversationResponse(id=conversation["id"], user_id=conversation["user_id"], title=conversation["title"], created_at=conversation["created_at"].isoformat(), updated_at=conversation["updated_at"].isoformat())


def validate_user_and_conversation(user_id: int, conversation_id: int) -> None:
    if not get_user_from_db(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    if not get_conversation_from_db(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
