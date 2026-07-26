from ..config import KEEP_RECENT_TOKENS, MAX_RESPONSE_TOKENS, SUMMARY_CHUNK_MAX_TOKENS, SUMMARY_TRIGGER_TOKENS
from ..db import get_latest_summary_from_db, list_conversation_messages_after_from_db
from ..helper_functions import estimate_context_tokens, estimate_message_tokens
from ..prompts import BIOLOGY_SYSTEM_PROMPT
from ..states import ChatMessage, ChatState


def select_messages_for_summary(messages: list[ChatMessage]) -> list[ChatMessage]:
    if len(messages) <= 1:
        return []

    recent_tokens, recent_count = 0, 0
    for message in reversed(messages):
        message_tokens = estimate_message_tokens(message)
        if recent_count > 0 and recent_tokens + message_tokens > KEEP_RECENT_TOKENS:
            break
        recent_tokens += message_tokens
        recent_count += 1
    old_messages = messages[:-recent_count]
    if not old_messages:
        return []
    chunk: list[ChatMessage] = []
    chunk_tokens = 0
    for message in old_messages:
        message_tokens = estimate_message_tokens(message)
        if chunk and chunk_tokens + message_tokens > SUMMARY_CHUNK_MAX_TOKENS:
            break
        chunk.append(message)
        chunk_tokens += message_tokens
    return chunk


def load_context_snapshot(state: ChatState) -> dict:
    latest_summary = get_latest_summary_from_db(state["conversation_id"])
    summary = latest_summary["content"] if latest_summary else ""
    summary_cursor = latest_summary["covered_until_message_id"] if latest_summary else 0
    rows = list_conversation_messages_after_from_db(state["conversation_id"], summary_cursor)
    unsummarized_messages: list[ChatMessage] = [{"id": row["id"], "role": row["role"], "content": row["content"]} for row in rows]
    projected_messages: list[dict[str, str]] = [{"role": "system", "content": BIOLOGY_SYSTEM_PROMPT}]
    if summary:
        projected_messages.append({"role": "system", "content": "Summary of the earlier conversation:\n\n" + summary})
    projected_messages.extend({"role": message["role"], "content": message["content"]} for message in unsummarized_messages)
    projected_tokens = estimate_context_tokens(projected_messages) + MAX_RESPONSE_TOKENS
    summary_chunk = select_messages_for_summary(unsummarized_messages)
    return {"summary": summary, "summary_cursor": summary_cursor, "unsummarized_messages": unsummarized_messages, "projected_tokens": projected_tokens, "should_summarize": projected_tokens >= SUMMARY_TRIGGER_TOKENS, "can_summarize": bool(summary_chunk), "summarizable_message_count": len(summary_chunk)}
