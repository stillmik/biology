from ..config import KEEP_RECENT_TOKENS, MAX_RESPONSE_TOKENS, SUMMARY_CHUNK_MAX_TOKENS, SUMMARY_CONTEXT_MAX_TOKENS, SUMMARY_TRIGGER_TOKENS
from ..db import get_latest_summary_segment_from_db, list_conversation_messages_after_from_db, list_recent_summary_segments_within_token_budget_from_db
from ..helper_functions import estimate_context_tokens, estimate_message_tokens
from ..prompts import BIOLOGY_SYSTEM_PROMPT
from ..states import ChatMessage, ChatState


def select_summarizable_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    if len(messages) <= 1:
        return []
    recent_tokens, recent_count = 0, 0
    for message in reversed(messages):
        message_tokens = estimate_message_tokens(message)
        if recent_count > 0 and recent_tokens + message_tokens > KEEP_RECENT_TOKENS:
            break
        recent_tokens += message_tokens
        recent_count += 1
    return messages[:-recent_count]


def select_messages_for_summary(messages: list[ChatMessage]) -> list[ChatMessage]:
    older_messages = select_summarizable_messages(messages)
    if not older_messages:
        return []
    chunk: list[ChatMessage] = []
    chunk_tokens = 0
    for message in older_messages:
        message_tokens = estimate_message_tokens(message)
        if chunk and chunk_tokens + message_tokens > SUMMARY_CHUNK_MAX_TOKENS:
            break
        chunk.append(message)
        chunk_tokens += message_tokens
    return chunk


def format_summary_segment(segment: dict) -> dict[str, str]:
    return {"role": "system", "content": f"Conversation summary (messages {segment['covered_from_message_id']}-{segment['covered_until_message_id']}):\n\n{segment['content']}"}


def load_context_snapshot(state: ChatState) -> dict:
    latest_segment = get_latest_summary_segment_from_db(state["conversation_id"])
    summary_cursor = latest_segment["covered_until_message_id"] if latest_segment else 0
    attached_summaries = list_recent_summary_segments_within_token_budget_from_db(state["conversation_id"], SUMMARY_CONTEXT_MAX_TOKENS)
    included_summary = {"is_included": bool(attached_summaries), "segment_count": len(attached_summaries), "total_token_count": sum(segment["token_count"] for segment in attached_summaries), "segments": attached_summaries}
    rows = list_conversation_messages_after_from_db(state["conversation_id"], summary_cursor)
    unsummarized_messages: list[ChatMessage] = [{"id": row["id"], "role": row["role"], "content": row["content"]} for row in rows]
    projected_messages: list[dict[str, str]] = [{"role": "system", "content": BIOLOGY_SYSTEM_PROMPT}]
    projected_messages.extend(format_summary_segment(segment) for segment in attached_summaries)
    projected_messages.extend({"role": message["role"], "content": message["content"]} for message in unsummarized_messages)
    projected_tokens = estimate_context_tokens(projected_messages) + MAX_RESPONSE_TOKENS
    summarizable_messages = select_summarizable_messages(unsummarized_messages)
    summarizable_tokens = sum(estimate_message_tokens(message) for message in summarizable_messages)
    tokens_until_summarization = max(0, SUMMARY_TRIGGER_TOKENS - summarizable_tokens)
    summarization_trigger_progress = summarizable_tokens / SUMMARY_TRIGGER_TOKENS
    summary_chunk = select_messages_for_summary(unsummarized_messages)
    return {"attached_summaries": attached_summaries, "included_summary": included_summary, "summary_cursor": summary_cursor, "unsummarized_messages": unsummarized_messages, "projected_tokens": projected_tokens, "tokens_until_summarization": tokens_until_summarization, "summarization_trigger_progress": summarization_trigger_progress, "should_summarize": summarizable_tokens >= SUMMARY_TRIGGER_TOKENS, "can_summarize": bool(summary_chunk), "summarizable_message_count": len(summary_chunk)}
