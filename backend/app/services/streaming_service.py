import json
import logging
import time

from typing import Any, Iterator

from fastapi import HTTPException

from ..clients import get_xai_client
from ..config import MAX_RESPONSE_TOKENS, XAI_MODEL
from ..db import create_message_from_db, lock_conversation_from_db, count_messages_after_from_db, get_user_message_from_db, update_user_message_and_delete_following_from_db
from ..graph import prepare_graph
from ..helper_functions import create_initial_graph_state
from ..observability import MODEL_DURATION, MODEL_FIRST_TOKEN, MODEL_REQUESTS, STREAM_DELTAS, STREAM_OUTPUT_CHARACTERS, STREAMS, chatbot_trace, finish_chatbot_trace, hash_identifier, langgraph_config, log_event, observe_graph_execution, observe_operation, record_model_usage
from ..states import ChatRequest, ContextBudgetError


logger = logging.getLogger(__name__)


def create_stream_state() -> dict[str, Any]:
    return {"model_started_at": 0.0, "model_started": False, "stream_recorded": False, "chunks": [], "completed_response": None}


def create_stream_error_event(error_message: str) -> str:
    return "data: " + json.dumps({"error": error_message}) + "\n\n"


def stream_model_response_events(conversation_id: int, prepared_context: dict[str, Any], stream_state: dict[str, Any], operation: str) -> Iterator[str]:
    stream_state["model_started_at"] = time.perf_counter()
    stream_state["model_started"] = True

    log_event(logger, logging.INFO, "model_stream_started", conversation_id=conversation_id, model=XAI_MODEL, operation=operation)
    stream = get_xai_client().responses.create(model=XAI_MODEL, input=prepared_context["history"], max_output_tokens=MAX_RESPONSE_TOKENS, stream=True, langsmith_extra={"name": f"xai-{operation}", "tags": ["xai", "streaming", operation], "metadata": {"operation": operation, "model": XAI_MODEL}})
    first_token_seen = False

    for event in stream:
        event_type = getattr(event, "type", "")
        if event_type == "response.completed":
            stream_state["completed_response"] = getattr(event, "response", None)

        if event_type != "response.output_text.delta" or not event.delta:
            continue

        if not first_token_seen:
            first_token_seen = True
            first_token_duration = time.perf_counter() - stream_state["model_started_at"]
            MODEL_FIRST_TOKEN.labels(model=XAI_MODEL).observe(first_token_duration)
            log_event(logger, logging.INFO, "model_first_token", conversation_id=conversation_id, model=XAI_MODEL, first_token_seconds=round(first_token_duration, 6), operation=operation)

        stream_state["chunks"].append(event.delta)
        yield "data: " + json.dumps({"token": event.delta}) + "\n\n"


def save_streamed_model_response(user_id: int, conversation_id: int, stream_state: dict[str, Any], operation: str) -> str:
    reply = "".join(stream_state["chunks"])
    duration = time.perf_counter() - stream_state["model_started_at"]
    MODEL_REQUESTS.labels(model=XAI_MODEL, operation=operation, result="success").inc()
    MODEL_DURATION.labels(model=XAI_MODEL, operation=operation).observe(duration)

    if stream_state["completed_response"] is not None:
        record_model_usage(XAI_MODEL, operation, stream_state["completed_response"])

    STREAMS.labels(result="success").inc()
    STREAM_DELTAS.observe(len(stream_state["chunks"]))
    STREAM_OUTPUT_CHARACTERS.observe(len(reply))
    stream_state["stream_recorded"] = True
    create_message_from_db(user_id, conversation_id, "assistant", reply)
    log_event(logger, logging.INFO, "model_stream_completed", conversation_id=conversation_id, model=XAI_MODEL, duration_seconds=round(duration, 6), delta_count=len(stream_state["chunks"]), output_characters=len(reply), operation=operation)
    log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")
    return reply


def finish_streamed_chat_trace(trace_run: Any, prepared_context: dict[str, Any], stream_state: dict[str, Any], reply: str) -> None:
    finish_chatbot_trace(trace_run, {"reply": reply, "summary_decision": prepared_context["summary_decision"], "summary_reason": prepared_context["summary_reason"], "summary_passes": prepared_context["summary_passes"], "summary_cursor": prepared_context["summary_cursor"], "delta_count": len(stream_state["chunks"]), "output_characters": len(reply)})


def handle_stream_failure(conversation_id: int, stream_state: dict[str, Any], error: Exception, operation: str) -> str:
    if not stream_state["stream_recorded"]:
        STREAMS.labels(result="error").inc()

        if stream_state["model_started"]:
            MODEL_REQUESTS.labels(model=XAI_MODEL, operation=operation, result="error").inc()
            MODEL_DURATION.labels(model=XAI_MODEL, operation=operation).observe(time.perf_counter() - stream_state["model_started_at"])

    log_event(logger, logging.ERROR, "model_stream_failed", conversation_id=conversation_id, operation=operation, exception_type=type(error).__name__, exception_message=str(error))
    return create_stream_error_event("The model provider request failed")


def handle_stream_disconnection(conversation_id: int, stream_state: dict[str, Any], operation: str) -> None:
    if stream_state["stream_recorded"]:
        return

    STREAMS.labels(result="disconnected").inc()

    if stream_state["model_started"]:
        MODEL_REQUESTS.labels(model=XAI_MODEL, operation=operation, result="disconnected").inc()
    log_event(logger, logging.WARNING, "model_stream_disconnected", conversation_id=conversation_id, operation=operation, duration_seconds=round(time.perf_counter() - stream_state["model_started_at"], 6) if stream_state["model_started"] else 0)


def stream_chat_events(request: ChatRequest) -> Iterator[str]:
    stream_state = create_stream_state()
    try:
        with observe_operation("chat_stream"), lock_conversation_from_db(request.conversation_id):
            with chatbot_trace(request.user_id, request.conversation_id, "chat_stream", request.message) as trace_run:
                with observe_graph_execution("chat_stream"):
                    create_message_from_db(request.user_id, request.conversation_id, "user", request.message)
                    log_event(logger, logging.INFO, "message_saved", conversation_id=request.conversation_id, user_id_hash=hash_identifier(request.user_id), role="user")
                    prepared_context = prepare_graph.invoke(create_initial_graph_state(request.conversation_id), config=langgraph_config(request.user_id, request.conversation_id, "chat_stream"))
                    yield from stream_model_response_events(request.conversation_id, prepared_context, stream_state, "chat_stream")
                    reply = save_streamed_model_response(request.user_id, request.conversation_id, stream_state, "chat_stream")
                    finish_streamed_chat_trace(trace_run, prepared_context, stream_state, reply)
                yield "data: [DONE]\n\n"
    except ContextBudgetError as error:
        log_event(logger, logging.WARNING, "context_budget_exceeded", conversation_id=request.conversation_id)
        yield create_stream_error_event(str(error))
    except GeneratorExit:
        handle_stream_disconnection(request.conversation_id, stream_state, "chat_stream")
        raise
    except Exception as error:
        yield handle_stream_failure(request.conversation_id, stream_state, error, "chat_stream")


def stream_regenerated_message_events(message_id: int, user_id: int, content: str) -> Iterator[str]:
    message = get_user_message_from_db(message_id, user_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    if message["role"] != "user":
        raise HTTPException(status_code=409, detail="Only user messages can be edited")
    conversation_id = message["conversation_id"]
    stream_state = create_stream_state()
    try:
        with observe_operation("regenerate_message_stream"), lock_conversation_from_db(conversation_id):
            messages_deleted = count_messages_after_from_db(conversation_id, message_id)
            trace_metadata = {"edited_message_id": message_id, "messages_deleted": messages_deleted}
            with chatbot_trace(user_id, conversation_id, "message_regeneration", content, trace_metadata) as trace_run:
                updated_message = update_user_message_and_delete_following_from_db(message_id=message_id, user_id=user_id, new_content=content)
                if not updated_message:
                    raise HTTPException(status_code=404, detail="Message not found")
                log_event(logger, logging.INFO, "message_edited", conversation_id=conversation_id, message_id=message_id, user_id_hash=hash_identifier(user_id), messages_deleted=messages_deleted)
                graph_config = langgraph_config(user_id, conversation_id, "message_regeneration")
                graph_config["metadata"].update(trace_metadata)
                with observe_graph_execution("message_regeneration"):
                    prepared_context = prepare_graph.invoke(create_initial_graph_state(conversation_id), config=graph_config)
                    yield from stream_model_response_events(conversation_id, prepared_context, stream_state, "message_regeneration")
                    reply = save_streamed_model_response(user_id, conversation_id, stream_state, "message_regeneration")
                    finish_streamed_chat_trace(trace_run, prepared_context, stream_state, reply)
                yield "data: [DONE]\n\n"
    except ContextBudgetError as error:
        log_event(logger, logging.WARNING, "context_budget_exceeded", conversation_id=conversation_id)
        yield create_stream_error_event(str(error))
    except GeneratorExit:
        handle_stream_disconnection(conversation_id, stream_state, "message_regeneration")
        raise
    except HTTPException as error:
        yield create_stream_error_event(error.detail)
    except Exception as error:
        yield handle_stream_failure(conversation_id, stream_state, error, "message_regeneration")
