import logging
import os
import time

from dotenv import load_dotenv
from langsmith.wrappers import wrap_openai
from openai import OpenAI

from ..core.clients import close_xai_client, set_xai_client
from ..core.config import DOCUMENT_WORKER_POLL_SECONDS, XAI_MODEL
from ..core.observability import configure_logging, langsmith_tracing_extra, log_event
from ..infrastructure.database import initialize_database_from_db
from ..infrastructure.document_repository import (
    claim_document_analysis_job_from_db,
    release_stale_document_analysis_jobs_from_db,
    retry_document_analysis_job_from_db,
)
from ..workflows.document_analysis import document_analysis_graph


load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


def process_document_analysis_job(job: dict) -> None:
    try:
        document_analysis_graph.invoke(
            {
                "job_id": job["id"],
                "document_id": job["document_id"],
            }
        )
        log_event(
            logger,
            logging.INFO,
            "document_analysis_completed",
            document_id=job["document_id"],
            analysis_job_id=job["id"],
        )
    except Exception as error:
        retry_job = retry_document_analysis_job_from_db(
            job["id"],
            type(error).__name__,
        )
        outcome = retry_job["status"] if retry_job else "missing"
        log_event(
            logger,
            logging.ERROR,
            "document_analysis_failed",
            document_id=job["document_id"],
            analysis_job_id=job["id"],
            outcome=outcome,
            exception_type=type(error).__name__,
        )


def run_document_worker() -> None:
    api_key = os.getenv("XAI_API_KEY")

    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured")

    initialize_database_from_db()
    release_stale_document_analysis_jobs_from_db()
    xai_client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    set_xai_client(wrap_openai(xai_client, tracing_extra=langsmith_tracing_extra()))
    log_event(logger, logging.INFO, "document_worker_started", model=XAI_MODEL)

    try:
        while True:
            job = claim_document_analysis_job_from_db()

            if job:
                process_document_analysis_job(job)
                continue

            time.sleep(DOCUMENT_WORKER_POLL_SECONDS)
    finally:
        close_xai_client()


if __name__ == "__main__":
    run_document_worker()
