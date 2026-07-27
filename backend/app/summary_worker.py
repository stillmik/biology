import logging
import os
import time

from dotenv import load_dotenv
from langsmith.wrappers import wrap_openai
from openai import OpenAI
from prometheus_client import start_http_server

from .clients import close_xai_client, set_xai_client
from .config import MAX_CONTEXT_TOKENS, SUMMARY_TRIGGER_TOKENS, SUMMARY_WORKER_METRICS_PORT, SUMMARY_WORKER_POLL_SECONDS, XAI_MODEL, XAI_SUMMARY_MODEL
from .db import claim_summary_job_from_db, complete_summary_job_from_db, list_summary_job_counts_from_db, lock_conversation_from_db, release_stale_summary_jobs_from_db, retry_summary_job_from_db
from .graph import summary_graph
from .helper_functions import create_initial_graph_state
from .observability import SUMMARY_JOB_DURATION, SUMMARY_JOB_QUEUE_DELAY, SUMMARY_JOBS, SUMMARY_JOB_STATUS, chatbot_trace, configure_logging, finish_chatbot_trace, langgraph_config, langsmith_tracing_extra, log_event, observe_graph_execution


load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


def refresh_summary_job_status_metrics() -> None:
    counts = {row["status"]: int(row["count"]) for row in list_summary_job_counts_from_db()}
    for status in ("queued", "running", "completed", "failed", "cancelled"):
        SUMMARY_JOB_STATUS.labels(status=status).set(counts.get(status, 0))


def process_summary_job(job: dict) -> None:
    started = time.perf_counter()
    queue_delay = max(0.0, (job["claimed_at"] - job["created_at"]).total_seconds())
    SUMMARY_JOB_QUEUE_DELAY.observe(queue_delay)
    SUMMARY_JOBS.labels(action="claimed", result="success").inc()
    metadata = {"summary_job_id": job["id"], "source_message_id": job["source_message_id"], "source_trace_id": job["source_trace_id"], "attempt_count": job["attempt_count"]}
    try:
        with lock_conversation_from_db(job["conversation_id"]):
            with chatbot_trace(0, job["conversation_id"], "post_response_summarization", f"summary-job:{job['id']}", metadata) as trace_run:
                with observe_graph_execution("post_response_summarization"):
                    result = summary_graph.invoke(create_initial_graph_state(job["conversation_id"]), config=langgraph_config(0, job["conversation_id"], "post_response_summarization"))
                complete_summary_job_from_db(job["id"])
                SUMMARY_JOBS.labels(action="completed", result="success").inc()
                finish_chatbot_trace(trace_run, {"summary_job_id": job["id"], "source_message_id": job["source_message_id"], "source_trace_id": job["source_trace_id"], "summary_decision": result["summary_decision"], "summary_reason": result["summary_reason"], "summary_passes": result["summary_passes"], "summary_cursor": result["summary_cursor"], "included_summary": result["included_summary"], "raw_message_tokens": result["raw_message_tokens"], "tokens_until_summarization": result["tokens_until_summarization"], "summarization_trigger_progress": result["summarization_trigger_progress"]})
                log_event(logger, logging.INFO, "summary_job_completed", conversation_id=job["conversation_id"], summary_job_id=job["id"], summary_passes=result["summary_passes"], decision=result["summary_decision"], duration_seconds=round(time.perf_counter() - started, 6))
    except Exception as error:
        retry_job = retry_summary_job_from_db(job["id"], type(error).__name__)
        outcome = retry_job["status"] if retry_job else "missing"
        SUMMARY_JOBS.labels(action="failed", result=outcome).inc()
        log_event(logger, logging.ERROR, "summary_job_failed", conversation_id=job["conversation_id"], summary_job_id=job["id"], attempt_count=job["attempt_count"], outcome=outcome, exception_type=type(error).__name__)
    finally:
        SUMMARY_JOB_DURATION.observe(time.perf_counter() - started)
        refresh_summary_job_status_metrics()


def run_summary_worker() -> None:
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured")
    set_xai_client(wrap_openai(OpenAI(api_key=api_key, base_url="https://api.x.ai/v1"), tracing_extra=langsmith_tracing_extra()))
    start_http_server(SUMMARY_WORKER_METRICS_PORT)
    release_stale_summary_jobs_from_db()
    log_event(logger, logging.INFO, "summary_worker_started", model=XAI_MODEL, summary_model=XAI_SUMMARY_MODEL, summary_trigger_tokens=SUMMARY_TRIGGER_TOKENS, max_context_tokens=MAX_CONTEXT_TOKENS, metrics_port=SUMMARY_WORKER_METRICS_PORT)
    try:
        while True:
            job = claim_summary_job_from_db()
            if job:
                process_summary_job(job)
            else:
                refresh_summary_job_status_metrics()
                time.sleep(SUMMARY_WORKER_POLL_SECONDS)
    finally:
        close_xai_client()


if __name__ == "__main__":
    run_summary_worker()
