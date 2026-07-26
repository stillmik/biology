import logging
import time

from typing import Literal

from ..config import MAX_CONTEXT_TOKENS, MAX_RESPONSE_TOKENS, MAX_SUMMARY_PASSES, SUMMARY_MAX_TOKENS, XAI_MODEL, XAI_SUMMARY_MODEL
from ..db import create_summary_from_db
from ..helper_functions import estimate_context_tokens, estimate_message_tokens, estimate_tokens, truncate_to_tokens
from ..observability import CONTEXT_BUDGET_WARNINGS, CONTEXT_TOKENS, GRAPH_BRANCHES, SUMMARIES, SUMMARY_DURATION, SUMMARY_MESSAGES, SUMMARY_REMAINING_MESSAGES, SUMMARY_TOKEN_REDUCTION, log_event, observe_graph_node
from ..prompts import BIOLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from ..services.model_service import generate_model_response
from ..states import ChatState, ContextBudgetError
from .context import load_context_snapshot, select_messages_for_summary


logger = logging.getLogger(__name__)


def load_context_node(state: ChatState) -> dict:
    with observe_graph_node("load_context"):
        result = load_context_snapshot(state)
    CONTEXT_TOKENS.labels(stage="loaded").observe(result["projected_tokens"])
    log_event(logger, logging.INFO, "context_loaded", conversation_id=state["conversation_id"], summary_cursor=result["summary_cursor"], unsummarized_message_count=len(result["unsummarized_messages"]), projected_tokens=result["projected_tokens"], summarizable_message_count=result["summarizable_message_count"])
    return result


def reload_context_node(state: ChatState) -> dict:
    with observe_graph_node("reload_context"):
        result = load_context_snapshot(state)
    CONTEXT_TOKENS.labels(stage="reloaded").observe(result["projected_tokens"])
    SUMMARY_REMAINING_MESSAGES.observe(len(result["unsummarized_messages"]))
    log_event(logger, logging.INFO, "context_reloaded", conversation_id=state["conversation_id"], summary_cursor=result["summary_cursor"], unsummarized_message_count=len(result["unsummarized_messages"]), projected_tokens=result["projected_tokens"], summary_passes=state["summary_passes"])
    return result


def needs_summary_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("needs_summary"):
        if not state["should_summarize"]:
            decision, reason = "build_context", "below_trigger_threshold"
        elif not state["can_summarize"]:
            decision, reason = "build_context", "no_summarizable_messages"
        elif state["summary_passes"] >= MAX_SUMMARY_PASSES:
            decision, reason = "build_context", "maximum_summary_passes_reached"
        else:
            decision, reason = "summarize", "context_above_trigger_threshold"
    GRAPH_BRANCHES.labels(branch=decision).inc()
    log_event(logger, logging.INFO, "summary_decision", conversation_id=state["conversation_id"], decision=decision, reason=reason, projected_tokens=state["projected_tokens"], summarizable_message_count=state["summarizable_message_count"], summary_passes=state["summary_passes"])
    return {"summary_decision": decision, "summary_reason": reason}


def summarize_node(state: ChatState) -> dict:
    started = time.perf_counter()
    with observe_graph_node("summarize"):
        chunk = select_messages_for_summary(state["unsummarized_messages"])
        if not chunk:
            SUMMARIES.labels(result="skipped").inc()
            return {"should_summarize": False, "can_summarize": False}
        transcript = "\n\n".join(f"{message['role'].upper()}: {message['content']}" for message in chunk)
        chunk_tokens = sum(estimate_message_tokens(message) for message in chunk)
        summary_request = f"PREVIOUS SUMMARY:\n\n{state['summary'] or '(No previous summary exists.)'}\n\nNEW OLDER MESSAGES:\n\n{transcript}\n\nReturn one replacement rolling summary."
        try:
            updated_summary = generate_model_response([{"role": "system", "content": SUMMARY_SYSTEM_PROMPT}, {"role": "user", "content": summary_request}], model=XAI_SUMMARY_MODEL, max_output_tokens=SUMMARY_MAX_TOKENS, operation="summarization")
            updated_summary = truncate_to_tokens(updated_summary, SUMMARY_MAX_TOKENS)
            summary_tokens = estimate_tokens(updated_summary)
            token_reduction = max(0, chunk_tokens - summary_tokens)
            create_summary_from_db(state["conversation_id"], updated_summary, summary_tokens, chunk[-1]["id"])
        except Exception:
            SUMMARIES.labels(result="error").inc()
            raise
    duration = time.perf_counter() - started
    SUMMARIES.labels(result="success").inc()
    SUMMARY_DURATION.observe(duration)
    SUMMARY_MESSAGES.observe(len(chunk))
    SUMMARY_TOKEN_REDUCTION.observe(token_reduction)
    log_event(logger, logging.INFO, "summary_completed", conversation_id=state["conversation_id"], previous_summary_cursor=state["summary_cursor"], new_summary_cursor=chunk[-1]["id"], summarized_message_count=len(chunk), summary_pass=state["summary_passes"] + 1, input_token_estimate=chunk_tokens, summary_token_estimate=summary_tokens, token_reduction=token_reduction, duration_seconds=round(duration, 6))
    return {"summary_passes": state["summary_passes"] + 1, "summary_messages_processed": len(chunk), "summary_token_reduction": token_reduction}


def build_context_node(state: ChatState) -> dict:
    with observe_graph_node("build_context"):
        history: list[dict[str, str]] = [{"role": "system", "content": BIOLOGY_SYSTEM_PROMPT}]
        if state["summary"]:
            history.append({"role": "system", "content": "Summary of the earlier conversation:\n\n" + state["summary"]})
        history.extend({"role": message["role"], "content": message["content"]} for message in state["unsummarized_messages"])
        projected_total = estimate_context_tokens(history) + MAX_RESPONSE_TOKENS
        CONTEXT_TOKENS.labels(stage="final").observe(projected_total)
        if projected_total > MAX_CONTEXT_TOKENS:
            CONTEXT_BUDGET_WARNINGS.inc()
            raise ContextBudgetError("Conversation context is too large and could not be compressed within the configured summary-pass limit.")
    log_event(logger, logging.INFO, "context_built", conversation_id=state["conversation_id"], summary_cursor=state["summary_cursor"], history_message_count=len(history), unsummarized_message_count=len(state["unsummarized_messages"]), projected_tokens=projected_total)
    return {"history": history}


def grok_node(state: ChatState) -> dict[str, str]:
    with observe_graph_node("grok"):
        reply = generate_model_response(state["history"], model=XAI_MODEL, max_output_tokens=MAX_RESPONSE_TOKENS, operation="chat")
    return {"reply": reply}


def context_route(state: ChatState) -> Literal["summarize", "build_context"]:
    return state["summary_decision"]
