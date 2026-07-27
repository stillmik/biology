import json
import logging
import time

import contextvars

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Iterator

from fastapi import HTTPException

from ..core.clients import get_xai_client
from ..core.config import MAX_FILE_CONTENT_SIZE, MAX_FILE_DESCRIPTION_TOKENS, MAX_PARALLEL_FILE_GENERATIONS, MAX_RESPONSE_TOKENS, XAI_MODEL
from ..core.observability import CONTEXT_PREPARATION_DURATION, CONTEXT_SAFETY_FALLBACKS, MODEL_DURATION, MODEL_FIRST_TOKEN, MODEL_INCOMPLETE, MODEL_REQUESTS, REQUEST_TO_FIRST_TOKEN, STREAM_DELTAS, STREAM_OUTPUT_CHARACTERS, STREAMS, chatbot_trace, finish_chatbot_trace, get_chatbot_trace_id, hash_identifier, langgraph_config, log_event, observe_graph_execution, observe_operation, record_model_usage
from ..infrastructure.database import count_messages_after_from_db, create_message_from_db, enqueue_summary_job_from_db, get_user_message_from_db, lock_conversation_from_db, update_user_message_and_delete_following_from_db
from ..schemas.chat import ChatRequest, ContextBudgetError
from .response_file_service import create_generated_response_file
from ..utils.chat_context import create_initial_graph_state
from ..workflows.graph import file_generation_graph, prepare_graph, safety_context_graph


logger = logging.getLogger(__name__)
FILE_GENERATION_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_PARALLEL_FILE_GENERATIONS, thread_name_prefix="biology-file-generation")


def create_stream_state() -> dict[str, Any]:
    return {"request_started_at": time.perf_counter(), "context_preparation_seconds": 0.0, "model_started_at": 0.0, "model_started": False, "model_first_token_seconds": None, "request_to_first_token_seconds": None, "stream_recorded": False, "chunks": [], "completed_response": None}


def create_stream_error_event(error_message: str) -> str:
    return "data: " + json.dumps({"error": error_message}) + "\n\n"


def get_model_finish_reason(response: Any) -> str:
    if response is None:
        return "unknown"
    incomplete_details = getattr(response, "incomplete_details", None)
    reason = getattr(incomplete_details, "reason", None) if incomplete_details is not None else None
    return str(reason or getattr(response, "status", None) or "unknown")


def prepare_stream_answer_context(user_id: int, conversation_id: int, operation: str, started_at: float, graph_metadata: dict[str, Any] | None = None, generate_file: bool = False) -> dict[str, Any]:
    graph_config = langgraph_config(user_id, conversation_id, operation)
    graph_config["metadata"].update(graph_metadata or {})
    try:
        with observe_graph_execution(operation):
            prepared_context = prepare_graph.invoke(create_initial_graph_state(conversation_id, generate_file), config=graph_config)
    except ContextBudgetError:
        CONTEXT_SAFETY_FALLBACKS.inc()
        log_event(logger, logging.WARNING, "context_safety_fallback_started", conversation_id=conversation_id, operation=operation)
        with observe_graph_execution(f"{operation}_safety_fallback"):
            prepared_context = safety_context_graph.invoke(create_initial_graph_state(conversation_id, generate_file), config=graph_config)
        prepared_context["context_budget_result"] = "safety_compressed"
    duration = time.perf_counter() - started_at
    CONTEXT_PREPARATION_DURATION.labels(operation=operation).observe(duration)
    log_event(logger, logging.INFO, "answer_context_preparation_completed", conversation_id=conversation_id, operation=operation, context_preparation_seconds=round(duration, 6), attached_segment_count=prepared_context["included_summary"]["segment_count"], attached_summary_tokens=prepared_context["included_summary"]["total_token_count"], raw_message_count=len(prepared_context["unsummarized_messages"]), raw_message_tokens=prepared_context["raw_message_tokens"], prompt_token_estimate=prepared_context["projected_tokens"], context_budget_result=prepared_context["context_budget_result"])
    return prepared_context


def generate_file_content(user_id: int, conversation_id: int, prepared_context: dict[str, Any]) -> str:
    graph_config = langgraph_config(user_id, conversation_id, "file_generation")
    with observe_graph_execution("file_generation"):
        result = file_generation_graph.invoke({**prepared_context, "generate_file": True}, config=graph_config)
    return result["file_content"]


def start_parallel_file_generation(user_id: int, conversation_id: int, prepared_context: dict[str, Any]) -> Future[str]:
    tracing_context = contextvars.copy_context()
    future = FILE_GENERATION_EXECUTOR.submit(tracing_context.run, generate_file_content, user_id, conversation_id, prepared_context.copy())
    log_event(logger, logging.INFO, "file_generation_started", conversation_id=conversation_id, execution_mode="parallel")
    return future


def cancel_parallel_file_generation(future: Future[str] | None) -> None:
    if future is not None and not future.done():
        future.cancel()


def stream_model_response_events(conversation_id: int, prepared_context: dict[str, Any], stream_state: dict[str, Any], operation: str, max_output_tokens: int = MAX_RESPONSE_TOKENS) -> Iterator[str]:
    stream_state["model_started_at"] = time.perf_counter()
    stream_state["model_started"] = True
    log_event(logger, logging.INFO, "model_stream_started", conversation_id=conversation_id, model=XAI_MODEL, operation=operation)
    stream = get_xai_client().responses.create(model=XAI_MODEL, input=prepared_context["history"], max_output_tokens=max_output_tokens, stream=True, langsmith_extra={"name": f"xai-{operation}", "tags": ["xai", "streaming", operation], "metadata": {"operation": operation, "model": XAI_MODEL, "max_output_tokens": max_output_tokens}})
    first_token_seen = False
    for event in stream:
        event_type = getattr(event, "type", "")
        if event_type == "response.completed":
            stream_state["completed_response"] = getattr(event, "response", None)
        if event_type != "response.output_text.delta" or not event.delta:
            continue
        if not first_token_seen:
            first_token_seen = True
            model_first_token_seconds = time.perf_counter() - stream_state["model_started_at"]
            request_to_first_token_seconds = time.perf_counter() - stream_state["request_started_at"]
            stream_state["model_first_token_seconds"] = model_first_token_seconds
            stream_state["request_to_first_token_seconds"] = request_to_first_token_seconds
            MODEL_FIRST_TOKEN.labels(model=XAI_MODEL).observe(model_first_token_seconds)
            REQUEST_TO_FIRST_TOKEN.labels(operation=operation).observe(request_to_first_token_seconds)
            log_event(logger, logging.INFO, "model_first_token", conversation_id=conversation_id, model=XAI_MODEL, operation=operation, model_first_token_seconds=round(model_first_token_seconds, 6), request_to_first_token_seconds=round(request_to_first_token_seconds, 6), context_preparation_seconds=round(stream_state["context_preparation_seconds"], 6))
        stream_state["chunks"].append(event.delta)
        yield "data: " + json.dumps({"token": event.delta}) + "\n\n"


def save_streamed_model_response(user_id: int, conversation_id: int, stream_state: dict[str, Any], operation: str, source_trace_id: str, request_message: str = "", generate_file: bool = False, file_content: str = "") -> tuple[str, dict, dict | None]:
    reply = "".join(stream_state["chunks"])
    duration = time.perf_counter() - stream_state["model_started_at"]
    MODEL_REQUESTS.labels(model=XAI_MODEL, operation=operation, result="success").inc()
    MODEL_DURATION.labels(model=XAI_MODEL, operation=operation).observe(duration)
    finish_reason = get_model_finish_reason(stream_state["completed_response"])

    if stream_state["completed_response"] is not None:
        record_model_usage(XAI_MODEL, operation, stream_state["completed_response"])

    if finish_reason in {"max_output_tokens", "length", "incomplete"}:
        MODEL_INCOMPLETE.labels(model=XAI_MODEL, operation=operation, reason=finish_reason).inc()

    STREAMS.labels(result="success").inc()
    STREAM_DELTAS.observe(len(stream_state["chunks"]))
    STREAM_OUTPUT_CHARACTERS.observe(len(reply))
    stream_state["stream_recorded"] = True
    assistant_message = create_message_from_db(user_id, conversation_id, "assistant", reply)

    generated_file = create_generated_response_file(user_id, conversation_id, assistant_message["id"], request_message, file_content) if generate_file else None
    summary_job = enqueue_summary_job_from_db(conversation_id, assistant_message["id"], source_trace_id)
    log_event(logger, logging.INFO, "model_stream_completed", conversation_id=conversation_id, model=XAI_MODEL, duration_seconds=round(duration, 6), delta_count=len(stream_state["chunks"]), output_characters=len(reply), finish_reason=finish_reason, operation=operation)
    log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")

    if generated_file:
        log_event(logger, logging.INFO, "response_file_generated", conversation_id=conversation_id, message_id=assistant_message["id"], generated_file_id=generated_file["id"], mime_type=generated_file["mime_type"])

    log_event(logger, logging.INFO, "summary_job_enqueued", conversation_id=conversation_id, summary_job_id=summary_job["id"], summary_job_status=summary_job["status"], source_message_id=assistant_message["id"])
    stream_state["finish_reason"] = finish_reason
    return reply, summary_job, generated_file


def finish_streamed_chat_trace(trace_run: Any, prepared_context: dict[str, Any], stream_state: dict[str, Any], reply: str, summary_job: dict | None = None, extra: dict[str, Any] | None = None) -> None:
    finish_chatbot_trace(trace_run, {"reply": reply, "summary_decision": prepared_context["summary_decision"], "summary_reason": prepared_context["summary_reason"], "included_summary": prepared_context["included_summary"], "summary_passes": prepared_context["summary_passes"], "summary_cursor": prepared_context["summary_cursor"], "raw_message_tokens": prepared_context["raw_message_tokens"], "projected_tokens": prepared_context["projected_tokens"], "tokens_until_summarization": prepared_context["tokens_until_summarization"], "summarization_trigger_progress": prepared_context["summarization_trigger_progress"], "context_budget_result": prepared_context["context_budget_result"], "context_preparation_seconds": stream_state["context_preparation_seconds"], "request_to_first_token_seconds": stream_state["request_to_first_token_seconds"], "model_first_token_seconds": stream_state["model_first_token_seconds"], "delta_count": len(stream_state["chunks"]), "output_characters": len(reply), "finish_reason": stream_state.get("finish_reason", "unknown"), "summary_job_id": summary_job["id"] if summary_job else None, **(extra or {})})


def handle_stream_failure(conversation_id: int, stream_state: dict[str, Any], error: Exception, operation: str) -> str:
    if not stream_state["stream_recorded"]:
        STREAMS.labels(result="error").inc()
        if stream_state["model_started"]:
            MODEL_REQUESTS.labels(model=XAI_MODEL, operation=operation, result="error").inc()
            MODEL_DURATION.labels(model=XAI_MODEL, operation=operation).observe(time.perf_counter() - stream_state["model_started_at"])
    log_event(logger, logging.ERROR, "model_stream_failed", conversation_id=conversation_id, operation=operation, exception_type=type(error).__name__)
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
    file_generation_future: Future[str] | None = None
    try:
        with observe_operation("chat_stream"), lock_conversation_from_db(request.conversation_id):
            with chatbot_trace(request.user_id, request.conversation_id, "chat_stream", request.message) as trace_run:
                user_message = create_message_from_db(request.user_id, request.conversation_id, "user", request.message)
                log_event(logger, logging.INFO, "message_saved", conversation_id=request.conversation_id, user_id_hash=hash_identifier(request.user_id), role="user")
                prepared_context = prepare_stream_answer_context(request.user_id, request.conversation_id, "chat_stream", stream_state["request_started_at"], generate_file=request.generate_file)
                stream_state["context_preparation_seconds"] = time.perf_counter() - stream_state["request_started_at"]
                if request.generate_file:
                    file_generation_future = start_parallel_file_generation(request.user_id, request.conversation_id, prepared_context)
                yield from stream_model_response_events(request.conversation_id, prepared_context, stream_state, "chat_stream", MAX_FILE_DESCRIPTION_TOKENS if request.generate_file else MAX_RESPONSE_TOKENS)

                file_content = ""
                if file_generation_future is not None:
                    try:
                        file_content = file_generation_future.result()
                    except Exception as error:
                        log_event(logger, logging.ERROR, "file_generation_failed", conversation_id=request.conversation_id, exception_type=type(error).__name__)
                        reply, summary_job, generated_file = save_streamed_model_response(request.user_id, request.conversation_id, stream_state, "chat_stream", get_chatbot_trace_id(trace_run), request.message, False, "")
                        finish_streamed_chat_trace(trace_run, prepared_context, stream_state, reply, summary_job, {"source_message_id": user_message["id"], "generated_file": None, "file_generation_status": "failed"})
                        yield create_stream_error_event("The response was generated, but the file could not be created.")
                        yield "data: [DONE]\n\n"
                        return
                reply, summary_job, generated_file = save_streamed_model_response(request.user_id, request.conversation_id, stream_state, "chat_stream", get_chatbot_trace_id(trace_run), request.message, request.generate_file, file_content)
                finish_streamed_chat_trace(trace_run, prepared_context, stream_state, reply, summary_job, {"source_message_id": user_message["id"], "generated_file": generated_file})

                if generated_file:
                    yield "data: " + json.dumps({"file": generated_file}) + "\n\n"
                yield "data: [DONE]\n\n"
    except ContextBudgetError as error:
        log_event(logger, logging.WARNING, "context_budget_exceeded", conversation_id=request.conversation_id)
        yield create_stream_error_event(str(error))
    except GeneratorExit:
        handle_stream_disconnection(request.conversation_id, stream_state, "chat_stream")
        raise
    except Exception as error:
        yield handle_stream_failure(request.conversation_id, stream_state, error, "chat_stream")
    finally:
        cancel_parallel_file_generation(file_generation_future)


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
                prepared_context = prepare_stream_answer_context(user_id, conversation_id, "message_regeneration", stream_state["request_started_at"], trace_metadata)
                stream_state["context_preparation_seconds"] = time.perf_counter() - stream_state["request_started_at"]
                yield from stream_model_response_events(conversation_id, prepared_context, stream_state, "message_regeneration")
                reply, summary_job, _ = save_streamed_model_response(user_id, conversation_id, stream_state, "message_regeneration", get_chatbot_trace_id(trace_run))
                finish_streamed_chat_trace(trace_run, prepared_context, stream_state, reply, summary_job, trace_metadata)
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
