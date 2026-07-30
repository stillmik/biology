import logging
import os
import time

from dotenv import load_dotenv
from langsmith.wrappers import wrap_openai
from openai import OpenAI

from ..core.clients import close_xai_client, set_xai_client
from ..core.config import ANSWER_WORKER_POLL_SECONDS, XAI_MODEL
from ..core.observability import configure_logging, langsmith_tracing_extra, log_event
from ..infrastructure.database import initialize_database_from_db, lock_conversation_from_db
from ..infrastructure.document_repository import (
    claim_answer_job_from_db,
    list_answer_job_documents_from_db,
    release_stale_answer_jobs_from_db,
    retry_answer_job_from_db,
)
from ..services.document_answer_service import generate_and_save_document_answer


load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


def process_answer_job(job: dict) -> None:
    try:
        documents = list_answer_job_documents_from_db(job["id"])

        if not documents or any(document["status"] != "ready" for document in documents):
            raise RuntimeError("Answer job was released before all documents were ready")

        with lock_conversation_from_db(job["conversation_id"]):
            generate_and_save_document_answer(
                job["user_id"],
                job["conversation_id"],
                job["question"],
                documents,
                job["id"],
            )
        log_event(
            logger,
            logging.INFO,
            "document_answer_job_completed",
            answer_job_id=job["id"],
            conversation_id=job["conversation_id"],
        )
    except Exception as error:
        retry_job = retry_answer_job_from_db(job["id"], type(error).__name__)
        outcome = retry_job["status"] if retry_job else "missing"
        log_event(
            logger,
            logging.ERROR,
            "document_answer_job_failed",
            answer_job_id=job["id"],
            conversation_id=job["conversation_id"],
            outcome=outcome,
            exception_type=type(error).__name__,
        )


def run_answer_worker() -> None:
    api_key = os.getenv("XAI_API_KEY")

    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured")

    initialize_database_from_db()
    release_stale_answer_jobs_from_db()
    xai_client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    set_xai_client(wrap_openai(xai_client, tracing_extra=langsmith_tracing_extra()))
    log_event(logger, logging.INFO, "answer_worker_started", model=XAI_MODEL)

    try:
        while True:
            job = claim_answer_job_from_db()

            if job:
                process_answer_job(job)
                continue

            time.sleep(ANSWER_WORKER_POLL_SECONDS)
    finally:
        close_xai_client()


if __name__ == "__main__":
    run_answer_worker()
