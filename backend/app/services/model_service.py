import logging
import time

from ..core.clients import get_xai_client
from ..core.observability import MODEL_DURATION, MODEL_REQUESTS, log_event, record_model_usage


logger = logging.getLogger(__name__)


def generate_model_response(model_input: list[dict[str, str]], *, model: str, max_output_tokens: int, operation: str) -> str:
    started = time.perf_counter()
    log_event(logger, logging.INFO, "model_request_started", model=model, operation=operation)

    try:
        response = get_xai_client().responses.create(model=model, input=model_input, max_output_tokens=max_output_tokens, langsmith_extra={"name": f"xai-{operation}", "tags": ["xai", operation], "metadata": {"operation": operation, "model": model}})

    except Exception:
        MODEL_REQUESTS.labels(model=model, operation=operation, result="error").inc()
        log_event(logger, logging.ERROR, "model_request_failed", model=model, operation=operation, duration_seconds=round(time.perf_counter() - started, 6))
        raise

    duration = time.perf_counter() - started
    MODEL_REQUESTS.labels(model=model, operation=operation, result="success").inc()
    MODEL_DURATION.labels(model=model, operation=operation).observe(duration)
    record_model_usage(model, operation, response)
    log_event(logger, logging.INFO, "model_request_completed", model=model, operation=operation, duration_seconds=round(duration, 6))
    return response.output_text.strip()
