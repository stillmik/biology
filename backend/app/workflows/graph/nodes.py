import logging
import time

from typing import Literal

from ...core.config import MAX_CONTEXT_TOKENS, MAX_FILE_CONTENT_SIZE, MAX_FILE_DESCRIPTION_TOKENS, MAX_RESPONSE_TOKENS, MAX_SUMMARY_PASSES, SUMMARY_MAX_TOKENS, XAI_MODEL, XAI_SUMMARY_MODEL
from ...core.observability import CONTEXT_BUDGET_WARNINGS, CONTEXT_SUMMARIZATION_TRIGGER_PROGRESS, CONTEXT_TOKENS, CONTEXT_TOKENS_UNTIL_SUMMARIZATION, GRAPH_BRANCHES, SUMMARIES, SUMMARY_DURATION, SUMMARY_MESSAGES, SUMMARY_REMAINING_MESSAGES, SUMMARY_TOKEN_REDUCTION, log_event, observe_graph_node
from ...infrastructure.database import create_summary_segment_from_db
from ...prompts import CHAT_RESPONSE_SYSTEM_PROMPT, CHAT_RESPONSE_WITH_FILE_SYSTEM_PROMPT, FILE_GENERATION_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from ...schemas.chat import ChatState, ContextBudgetError
from ...services.model_service import generate_model_response
from ...utils.chat_context import estimate_context_tokens, estimate_message_tokens, estimate_tokens
from .context import format_summary_segment, load_context_snapshot, select_messages_for_summary

logger = logging.getLogger(__name__)


def log_context_snapshot(event_name: str, state: ChatState, result: dict) -> None:
    log_event(logger, logging.INFO, event_name, conversation_id=state["conversation_id"], attached_summary_segment_count=result["included_summary"]["segment_count"], attached_summary_tokens=result["included_summary"]["total_token_count"], summary_cursor=result["summary_cursor"], unsummarized_message_count=len(result["unsummarized_messages"]), raw_message_tokens=result["raw_message_tokens"], projected_tokens=result["projected_tokens"], tokens_until_summarization=result["tokens_until_summarization"], summarization_trigger_progress=round(result["summarization_trigger_progress"], 4), summarizable_message_count=result["summarizable_message_count"])


def load_context_node(state: ChatState) -> dict:
    with observe_graph_node("load_context"):
        result = load_context_snapshot(state)
    CONTEXT_TOKENS.labels(stage="loaded").observe(result["projected_tokens"])
    CONTEXT_TOKENS_UNTIL_SUMMARIZATION.set(result["tokens_until_summarization"])
    CONTEXT_SUMMARIZATION_TRIGGER_PROGRESS.set(result["summarization_trigger_progress"])
    log_context_snapshot("context_loaded", state, result)
    return result


def reload_context_node(state: ChatState) -> dict:
    with observe_graph_node("reload_context"):
        result = load_context_snapshot(state)
    CONTEXT_TOKENS.labels(stage="reloaded").observe(result["projected_tokens"])
    CONTEXT_TOKENS_UNTIL_SUMMARIZATION.set(result["tokens_until_summarization"])
    CONTEXT_SUMMARIZATION_TRIGGER_PROGRESS.set(result["summarization_trigger_progress"])
    SUMMARY_REMAINING_MESSAGES.observe(len(result["unsummarized_messages"]))
    log_context_snapshot("context_reloaded", state, result)
    return result


def needs_summary_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("needs_summary"):
        if not state["should_summarize"]:
            decision, reason = "finish", "unsummarized_messages_below_trigger_threshold"
        elif not state["can_summarize"]:
            decision, reason = "finish", "no_summarizable_messages_after_recent_tail"
        elif state["summary_passes"] >= MAX_SUMMARY_PASSES:
            decision, reason = "finish", "maximum_summary_passes_reached"
        else:
            decision, reason = "summarize", "unsummarized_messages_above_trigger_threshold"
    GRAPH_BRANCHES.labels(branch=decision).inc()
    log_event(logger, logging.INFO, "summary_decision", conversation_id=state["conversation_id"], decision=decision, reason=reason, projected_tokens=state["projected_tokens"], tokens_until_summarization=state["tokens_until_summarization"], summarization_trigger_progress=round(state["summarization_trigger_progress"], 4), summarizable_message_count=state["summarizable_message_count"], summary_passes=state["summary_passes"])
    return {"summary_decision": decision, "summary_reason": reason}


def force_safety_summary_node(state: ChatState) -> dict[str, bool]:
    with observe_graph_node("force_safety_summary"):
        log_event(logger, logging.WARNING, "context_safety_summary_forced", conversation_id=state["conversation_id"], projected_tokens=state["projected_tokens"], raw_message_tokens=state["raw_message_tokens"])
    return {"should_summarize": True}


def summarize_node(state: ChatState) -> dict:
    started = time.perf_counter()
    with observe_graph_node("summarize"):
        chunk = select_messages_for_summary(state["unsummarized_messages"])
        if not chunk:
            SUMMARIES.labels(result="skipped").inc()
            return {"should_summarize": False, "can_summarize": False}
        transcript = "\n\n".join(f"{message['role'].upper()}: {message['content']}" for message in chunk)
        chunk_tokens = sum(estimate_message_tokens(message) for message in chunk)
        summary_request = f"MESSAGE RANGE {chunk[0]['id']}–{chunk[-1]['id']}:\n\n{transcript}\n\nReturn one standalone summary for only this message range."
        try:
            summary_content = generate_model_response([{"role": "system", "content": SUMMARY_SYSTEM_PROMPT}, {"role": "user", "content": summary_request}], model=XAI_SUMMARY_MODEL, max_output_tokens=SUMMARY_MAX_TOKENS, operation="summarization")
            summary_tokens = estimate_tokens(summary_content)
            token_reduction = max(0, chunk_tokens - summary_tokens)
            create_summary_segment_from_db(state["conversation_id"], summary_content, summary_tokens, chunk[0]["id"], chunk[-1]["id"])
        except Exception:
            SUMMARIES.labels(result="error").inc()
            raise
    duration = time.perf_counter() - started
    SUMMARIES.labels(result="success").inc()
    SUMMARY_DURATION.observe(duration)
    SUMMARY_MESSAGES.observe(len(chunk))
    SUMMARY_TOKEN_REDUCTION.observe(token_reduction)
    log_event(logger, logging.INFO, "summary_segment_completed", conversation_id=state["conversation_id"], covered_from_message_id=chunk[0]["id"], covered_until_message_id=chunk[-1]["id"], summarized_message_count=len(chunk), summary_pass=state["summary_passes"] + 1, input_token_estimate=chunk_tokens, summary_token_estimate=summary_tokens, token_reduction=token_reduction, duration_seconds=round(duration, 6))
    return {"summary_passes": state["summary_passes"] + 1, "summary_messages_processed": len(chunk), "summary_token_reduction": token_reduction}


def prepare_answer_context_node(state: ChatState) -> dict:
    with observe_graph_node("prepare_answer_context"):
        system_prompt = CHAT_RESPONSE_WITH_FILE_SYSTEM_PROMPT if state.get("generate_file") else CHAT_RESPONSE_SYSTEM_PROMPT
        history: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        history.extend(format_summary_segment(segment) for segment in state["attached_summaries"])
        history.extend({"role": message["role"], "content": message["content"]} for message in state["unsummarized_messages"])
        response_token_budget = MAX_FILE_DESCRIPTION_TOKENS if state.get("generate_file") else MAX_RESPONSE_TOKENS
        projected_total = estimate_context_tokens(history) + response_token_budget
        CONTEXT_TOKENS.labels(stage="final").observe(projected_total)
        if projected_total > MAX_CONTEXT_TOKENS:
            CONTEXT_BUDGET_WARNINGS.inc()
            raise ContextBudgetError("Conversation context is too large and requires safety compression.")
    log_event(logger, logging.INFO, "answer_context_prepared", conversation_id=state["conversation_id"], attached_summary_segment_count=len(state["attached_summaries"]), attached_summary_tokens=state["included_summary"]["total_token_count"], summary_cursor=state["summary_cursor"], history_message_count=len(history), unsummarized_message_count=len(state["unsummarized_messages"]), raw_message_tokens=state["raw_message_tokens"], projected_tokens=projected_total)
    return {"history": history, "context_budget_result": "within_limit"}


def mark_post_response_summary_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("mark_post_response_summary"):
        if state["should_summarize"] and state["can_summarize"]:
            decision, reason = "queued", "unsummarized_raw_messages_reached_trigger"
        elif state["should_summarize"]:
            decision, reason = "deferred", "recent_tail_has_no_summarizable_messages"
        else:
            decision, reason = "not_needed", "unsummarized_raw_messages_below_trigger_threshold"
    log_event(logger, logging.INFO, "post_response_summary_decision", conversation_id=state["conversation_id"], decision=decision, reason=reason, raw_message_tokens=state["raw_message_tokens"], tokens_until_summarization=state["tokens_until_summarization"])
    return {"summary_decision": decision, "summary_reason": reason}


def grok_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("grok"):
        response_token_budget = MAX_FILE_DESCRIPTION_TOKENS if state.get("generate_file") else MAX_RESPONSE_TOKENS
        reply = generate_model_response(state["history"], model=XAI_MODEL, max_output_tokens=response_token_budget, operation="chat")
    return {"reply": reply}


def file_generation_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("file_generation"):
        history = [{"role": "system", "content": FILE_GENERATION_SYSTEM_PROMPT}, *state["history"][1:]]
        file_content = generate_model_response(history, model=XAI_MODEL, max_output_tokens=MAX_FILE_CONTENT_SIZE, operation="file_generation")
    return {"file_content": file_content}


def context_route(state: ChatState) -> Literal["summarize", "finish"]:
    return "summarize" if state["summary_decision"] == "summarize" else "finish"
