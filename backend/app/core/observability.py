from __future__ import annotations


import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
import langsmith as ls


from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, TypeVar
from fastapi import Request, Response
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith.anonymizer import create_anonymizer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware


SERVICE_NAME = os.getenv("SERVICE_NAME", "biology-chat-backend")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
APP_VERSION = os.getenv("APP_VERSION", "0.3.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
HASH_SALT = os.getenv("OBSERVABILITY_HASH_SALT", "development-only-change-me")
LANGSMITH_ENABLED = os.getenv("LANGSMITH_TRACING", "false").lower() in {"1", "true", "yes", "on"} and bool(os.getenv("LANGSMITH_API_KEY"))
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "biology-chat")
LANGSMITH_SAMPLING_RATE = min(1.0, max(0.0, float(os.getenv("LANGSMITH_TRACING_SAMPLING_RATE", "1.0"))))


REQUEST_ID = contextvars.ContextVar("request_id", default="")


HTTP_REQUESTS = Counter("biology_http_requests_total", "HTTP requests completed by the backend.", ["method", "endpoint", "status_group"])
HTTP_DURATION = Histogram("biology_http_request_duration_seconds", "Backend HTTP request duration.", ["method", "endpoint"], buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
HTTP_IN_PROGRESS = Gauge("biology_http_requests_in_progress", "HTTP requests currently being processed.")
APP_OPERATIONS = Counter("biology_app_operations_total", "Application operations by result.", ["operation", "result"])
APP_OPERATION_DURATION = Histogram("biology_app_operation_duration_seconds", "Application operation duration.", ["operation"], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
GRAPH_EXECUTIONS = Counter("biology_langgraph_executions_total", "LangGraph executions by type and result.", ["execution_type", "result"])
GRAPH_EXECUTION_DURATION = Histogram("biology_langgraph_execution_duration_seconds", "LangGraph execution duration.", ["execution_type"], buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
GRAPH_NODE_EXECUTIONS = Counter("biology_langgraph_node_executions_total", "LangGraph node executions by result.", ["node", "result"])
GRAPH_NODE_DURATION = Histogram("biology_langgraph_node_duration_seconds", "LangGraph node duration.", ["node"], buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
GRAPH_BRANCHES = Counter("biology_langgraph_branches_total", "LangGraph conditional branch selections.", ["branch"])
CONTEXT_TOKENS = Histogram("biology_context_tokens", "Estimated context tokens by stage.", ["stage"], buckets=(100, 250, 500, 1000, 2000, 4000, 6000, 8000, 12000, 16000, 24000))
CONTEXT_TOKENS_UNTIL_SUMMARIZATION = Gauge("biology_context_tokens_until_summarization", "Estimated raw-message tokens remaining before the next summary-segment trigger.")
CONTEXT_SUMMARIZATION_TRIGGER_PROGRESS = Gauge("biology_context_summarization_trigger_progress_ratio", "Current unsummarized raw-message tokens as a ratio of the summary-segment trigger.")
CONTEXT_BUDGET_WARNINGS = Counter("biology_context_budget_warnings_total", "Context budget errors.")
SUMMARIES = Counter("biology_summaries_total", "Summary-segment attempts by result.", ["result"])
SUMMARY_DURATION = Histogram("biology_summary_duration_seconds", "Summary-segment model duration.", buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
SUMMARY_MESSAGES = Histogram("biology_summary_messages", "Messages included in one summary segment.", buckets=(1, 2, 3, 5, 8, 13, 21, 34, 55))
SUMMARY_REMAINING_MESSAGES = Histogram("biology_summary_remaining_messages", "Unsummarized messages remaining after a summary pass.", buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55))
SUMMARY_TOKEN_REDUCTION = Histogram("biology_summary_token_reduction", "Estimated tokens removed by summarization.", buckets=(0, 100, 250, 500, 1000, 2000, 4000, 8000, 12000))
MODEL_REQUESTS = Counter("biology_model_requests_total", "xAI model requests by operation and result.", ["model", "operation", "result"])
MODEL_DURATION = Histogram("biology_model_request_duration_seconds", "xAI model request duration.", ["model", "operation"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
MODEL_FIRST_TOKEN = Histogram("biology_model_first_token_seconds", "Time until the first streamed model delta.", ["model"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
CONTEXT_PREPARATION_DURATION = Histogram("biology_context_preparation_seconds", "Time from user-message persistence through complete answer-context construction.", ["operation"], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
REQUEST_TO_FIRST_TOKEN = Histogram("biology_request_to_first_token_seconds", "Time from backend request receipt through the first streamed text delta.", ["operation"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
MODEL_INPUT_TOKENS = Counter("biology_model_input_tokens_total", "Input tokens reported by xAI.", ["model", "operation"])
MODEL_OUTPUT_TOKENS = Counter("biology_model_output_tokens_total", "Output tokens reported by xAI.", ["model", "operation"])
MODEL_INCOMPLETE = Counter("biology_model_incomplete_total", "Model responses that ended before normal completion.", ["model", "operation", "reason"])
STREAMS = Counter("biology_streams_total", "Streaming responses by result.", ["result"])
STREAM_DELTAS = Histogram("biology_stream_delta_count", "Text deltas in a completed stream.", buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000))
STREAM_OUTPUT_CHARACTERS = Histogram("biology_stream_output_characters", "Characters in a completed streamed response.", buckets=(10, 50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000))
DB_OPERATIONS = Counter("biology_database_operations_total", "PostgreSQL operations by result.", ["operation", "result"])
DB_OPERATION_DURATION = Histogram("biology_database_operation_duration_seconds", "PostgreSQL operation duration.", ["operation"], buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
DB_CONNECTION_FAILURES = Counter("biology_database_connection_failures_total", "PostgreSQL connection acquisition failures.")
DB_CONNECTIONS_OPEN = Gauge("biology_database_connections_open", "Connections currently opened through the shared connection context.")
DB_LOCK_WAIT_DURATION = Histogram("biology_database_lock_wait_seconds", "Time spent waiting for a per-conversation PostgreSQL advisory lock.", buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
LANGSMITH_TRACES_ATTEMPTED = Counter("biology_langsmith_traces_attempted_total", "LangSmith chatbot root traces attempted by this process.", ["execution_type"])
LANGSMITH_EXPORT_FAILURES = Counter("biology_langsmith_export_failures_total", "Synchronous LangSmith tracing failures.")
SUMMARY_JOBS = Counter("biology_summary_jobs_total", "Background summary jobs by action and result.", ["action", "result"])
SUMMARY_JOB_DURATION = Histogram("biology_summary_job_duration_seconds", "Background summary-job duration.", buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
SUMMARY_JOB_QUEUE_DELAY = Histogram("biology_summary_job_queue_delay_seconds", "Delay from summary-job creation until worker claim.", buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900))
SUMMARY_JOB_STATUS = Gauge("biology_summary_jobs_current", "Summary jobs currently stored by status.", ["status"])
CONTEXT_SAFETY_FALLBACKS = Counter("biology_context_safety_fallbacks_total", "Synchronous safety compressions caused by hard context overflow.")


_STANDARD_LOG_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_SENSITIVE_KEYS = {"api_key", "authorization", "cookie", "database_url", "password", "secret", "access_token", "refresh_token"}
_TRACE_RULES = [{"pattern": re.compile(r"\bxai-\s*[A-Za-z0-9_-]{20,}\b"), "replace": "[REDACTED_XAI_KEY]"}, {"pattern": re.compile(r"\blsv2_[A-Za-z0-9_-]{20,}\b"), "replace": "[REDACTED_LANGSMITH_KEY]"}, {"pattern": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "replace": "[REDACTED_API_KEY]"}, {"pattern": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE), "replace": "Bearer [REDACTED]"}, {"pattern": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "replace": "[REDACTED_JWT]"}, {"pattern": re.compile(r"\bpostgres(?:ql)?(?:\+[A-Za-z0-9_-]+)?://[^\s\"']+", re.IGNORECASE), "replace": "[REDACTED_DATABASE_URL]"}]
_TRACE_ANONYMIZER = create_anonymizer(_TRACE_RULES)
_LANGSMITH_CLIENT: Client | None = None
_LANGSMITH_CLIENT_INITIALIZED = False
F = TypeVar("F", bound=Callable[..., Any])


def _redact_text(value: str) -> str:
    redacted = value

    for rule in _TRACE_RULES:
        redacted = rule["pattern"].sub(rule["replace"], redacted)
    return redacted


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}

    for key, value in fields.items():
        normalized_key = key.lower()
        sanitized[key] = "[REDACTED]" if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(("_api_key", "_password", "_secret", "_access_token", "_refresh_token")) else _json_safe(value)
    return sanitized


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "service": SERVICE_NAME, "environment": ENVIRONMENT, "app_version": APP_VERSION, "logger": record.name, "event": _redact_text(str(getattr(record, "event", record.getMessage()))), "message": _redact_text(record.getMessage()), "request_id": getattr(record, "request_id", REQUEST_ID.get())}
        payload.update(_sanitize_fields({key: value for key, value in record.__dict__.items() if key not in _STANDARD_LOG_FIELDS and key not in payload}))
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["stack_trace"] = _redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter() if LOG_FORMAT == "json" else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(LOG_LEVEL)
    logging.getLogger("uvicorn.access").disabled = True


def current_langsmith_trace_id() -> str:
    try:
        run = ls.get_current_run_tree()
        return str(run.trace_id) if run and run.trace_id else ""
    except Exception:
        return ""


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    trace_id = current_langsmith_trace_id()
    logger.log(level, event, extra={"event": event, "langsmith_trace_id": trace_id, **_sanitize_fields(fields)})


def anonymize_trace_data(value: Any) -> Any:
    return _TRACE_ANONYMIZER(value)


def _langsmith_export_failed(error: Exception) -> None:
    LANGSMITH_EXPORT_FAILURES.inc()
    log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_export_failed", exception_type=type(error).__name__)


def get_langsmith_client() -> Client | None:
    global _LANGSMITH_CLIENT, _LANGSMITH_CLIENT_INITIALIZED

    if not LANGSMITH_ENABLED:
        return None

    if _LANGSMITH_CLIENT_INITIALIZED:
        return _LANGSMITH_CLIENT
    _LANGSMITH_CLIENT_INITIALIZED = True

    try:
        _LANGSMITH_CLIENT = Client(api_key=os.getenv("LANGSMITH_API_KEY"), workspace_id=os.getenv("LANGSMITH_WORKSPACE_ID") or None, anonymizer=_TRACE_ANONYMIZER, tracing_sampling_rate=LANGSMITH_SAMPLING_RATE, tracing_error_callback=_langsmith_export_failed)
    except Exception as error:
        LANGSMITH_EXPORT_FAILURES.inc()
        log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_client_initialization_failed", exception_type=type(error).__name__)
    return _LANGSMITH_CLIENT


def langsmith_tracing_extra() -> dict[str, Any] | None:
    client = get_langsmith_client()
    return {"client": client} if client is not None else None


def hash_identifier(value: int | str) -> str:
    return hmac.new(HASH_SALT.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """
    inherits from FastAPI or Starlette's BaseHTTPMiddleware. 
    That means FastAPI calls its dispatch() method for every request. 
    
    request arrives
      ↓
    generate request ID
        ↓
    start timer
        ↓
    mark request as active
        ↓
    log request_started
        ↓
    run the real endpoint
        ↓
    record response or exception
        ↓
    update metrics
        ↓
    log request_completed
        ↓
    clean up request context
    """
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = str(uuid.uuid4())
        request_token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status_code = 500
        HTTP_IN_PROGRESS.inc()
        log_event(logging.getLogger("biology.http"), logging.INFO, "request_started", method=request.method)

        try:
            # @app.post("/api/chat")
            response = await call_next(request)
            # after @app.post("/api/chat")
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            log_event(logging.getLogger("biology.http"), logging.ERROR, "request_failed", method=request.method, endpoint=route_template(request), exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "Exception")
            raise
        finally:
            endpoint = route_template(request)
            duration = time.perf_counter() - started
            HTTP_IN_PROGRESS.dec()

            if METRICS_ENABLED and endpoint != "/metrics":
                HTTP_REQUESTS.labels(method=request.method, endpoint=endpoint, status_group=f"{status_code // 100}xx").inc()
                HTTP_DURATION.labels(method=request.method, endpoint=endpoint).observe(duration)

            log_event(logging.getLogger("biology.http"), logging.INFO, "request_completed", method=request.method, endpoint=endpoint, status_code=status_code, duration_seconds=round(duration, 6))
            REQUEST_ID.reset(request_token)


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@contextmanager
def observe_operation(operation: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    except Exception:
        APP_OPERATIONS.labels(operation=operation, result="error").inc()
        raise
    else:
        APP_OPERATIONS.labels(operation=operation, result="success").inc()
    finally:
        APP_OPERATION_DURATION.labels(operation=operation).observe(time.perf_counter() - started)


def observe_database_operation(operation: str) -> Callable[[F], F]:
    def decorator(function: F) -> F:
        @functools.wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                result_label = "conflict" if type(error).__name__ == "UniqueViolation" else "error"
                DB_OPERATIONS.labels(operation=operation, result=result_label).inc()
                log_event(logging.getLogger("biology.database"), logging.WARNING if result_label == "conflict" else logging.ERROR, "database_operation_conflict" if result_label == "conflict" else "database_operation_failed", operation=operation, duration_seconds=round(time.perf_counter() - started, 6), exception_type=type(error).__name__)
                raise
            else:
                DB_OPERATIONS.labels(operation=operation, result="success").inc()
                return result
            finally:
                DB_OPERATION_DURATION.labels(operation=operation).observe(time.perf_counter() - started)
        return wrapped  # type: ignore[return-value]
    return decorator


@contextmanager
def observe_graph_node(node: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    except Exception:
        GRAPH_NODE_EXECUTIONS.labels(node=node, result="error").inc()
        log_event(logging.getLogger("biology.langgraph"), logging.ERROR, "graph_node_failed", graph_node=node, duration_seconds=round(time.perf_counter() - started, 6), exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "Exception")
        raise
    else:
        GRAPH_NODE_EXECUTIONS.labels(node=node, result="success").inc()
        log_event(logging.getLogger("biology.langgraph"), logging.INFO, "graph_node_completed", graph_node=node, duration_seconds=round(time.perf_counter() - started, 6))
    finally:
        GRAPH_NODE_DURATION.labels(node=node).observe(time.perf_counter() - started)


@contextmanager
def observe_graph_execution(execution_type: str) -> Iterator[None]:
    started = time.perf_counter()
    log_event(logging.getLogger("biology.langgraph"), logging.INFO, "graph_started", execution_type=execution_type)

    try:
        yield
    except Exception:
        GRAPH_EXECUTIONS.labels(execution_type=execution_type, result="error").inc()
        log_event(logging.getLogger("biology.langgraph"), logging.ERROR, "graph_failed", execution_type=execution_type, duration_seconds=round(time.perf_counter() - started, 6), exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "Exception")
        raise
    else:
        GRAPH_EXECUTIONS.labels(execution_type=execution_type, result="success").inc()
        log_event(logging.getLogger("biology.langgraph"), logging.INFO, "graph_completed", execution_type=execution_type, duration_seconds=round(time.perf_counter() - started, 6))
    finally:
        GRAPH_EXECUTION_DURATION.labels(execution_type=execution_type).observe(time.perf_counter() - started)


def langgraph_config(user_id: int, conversation_id: int, execution_type: str) -> dict[str, Any]:
    metadata = {"thread_id": str(conversation_id), "conversation_id": str(conversation_id), "user_id_hash": hash_identifier(user_id), "request_id": REQUEST_ID.get(), "environment": ENVIRONMENT, "app_version": APP_VERSION, "model": os.getenv("XAI_MODEL", "grok-4.3"), "execution_type": execution_type}
    config: dict[str, Any] = {"run_name": f"biology-{execution_type}", "tags": [ENVIRONMENT, "biology-chat", execution_type], "metadata": metadata, "configurable": {"thread_id": str(conversation_id)}}
    client = get_langsmith_client()

    if client is not None:
        config["callbacks"] = [LangChainTracer(project_name=LANGSMITH_PROJECT, client=client)]
    return config


@contextmanager
def chatbot_trace(user_id: int, conversation_id: int, execution_type: str, message: str, metadata_extra: dict[str, Any] | None = None) -> Iterator[Any | None]:
    if not LANGSMITH_ENABLED:
        yield None
        return

    client = get_langsmith_client()

    if client is None:
        yield None
        return

    LANGSMITH_TRACES_ATTEMPTED.labels(execution_type=execution_type).inc()
    metadata = {**langgraph_config(user_id, conversation_id, execution_type)["metadata"], **(metadata_extra or {})}
    tags = [ENVIRONMENT, "biology-chat", execution_type]
    try:
        trace_context = ls.trace(name=f"biology-{execution_type}", run_type="chain", project_name=LANGSMITH_PROJECT, inputs={"message": message}, tags=tags, metadata={**metadata, "ls_agent_type": "root"}, client=client)
        run = trace_context.__enter__()
    except Exception:
        LANGSMITH_EXPORT_FAILURES.inc()
        log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_trace_start_failed", execution_type=execution_type, exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "Exception")
        yield None
        return
    try:
        yield run
    except BaseException:
        try:
            trace_context.__exit__(*sys.exc_info())
        except Exception:
            LANGSMITH_EXPORT_FAILURES.inc()
            log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_trace_finish_failed", execution_type=execution_type)
        raise
    else:
        try:
            trace_context.__exit__(None, None, None)
        except Exception:
            LANGSMITH_EXPORT_FAILURES.inc()
            log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_trace_finish_failed", execution_type=execution_type)


def finish_chatbot_trace(run: Any | None, outputs: dict[str, Any]) -> None:
    if run is None:
        return
    try:
        run.end(outputs=outputs)
    except Exception:
        LANGSMITH_EXPORT_FAILURES.inc()
        log_event(logging.getLogger("biology.langsmith"), logging.WARNING, "langsmith_trace_output_failed", exception_type=sys.exc_info()[0].__name__ if sys.exc_info()[0] else "Exception")


def get_chatbot_trace_id(run: Any | None) -> str:
    try:
        return str(getattr(run, "trace_id", "") or getattr(run, "id", "")) if run is not None else ""
    except Exception:
        return ""


def record_model_usage(model: str, operation: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    if input_tokens:
        MODEL_INPUT_TOKENS.labels(model=model, operation=operation).inc(input_tokens)
    if output_tokens:
        MODEL_OUTPUT_TOKENS.labels(model=model, operation=operation).inc(output_tokens)
