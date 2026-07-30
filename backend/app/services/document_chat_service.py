import json
import logging

from psycopg.errors import UniqueViolation

from ..core.observability import log_event
from ..infrastructure.document_repository import (
    create_queued_document_question_from_db,
    list_conversation_documents_from_db,
)
from ..schemas.chat import ChatRequest


logger = logging.getLogger(__name__)


def create_sse_event(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def stream_queued_document_question_events(
    request: ChatRequest,
    documents: list[dict],
):
    document_ids = [document["id"] for document in documents]

    try:
        user_message, answer_job = create_queued_document_question_from_db(
            request.user_id,
            request.conversation_id,
            request.message,
            document_ids,
        )
    except UniqueViolation:
        yield create_sse_event(
            {"error": "Wait for the current document answer to finish before asking another question."}
        )
        yield "data: [DONE]\n\n"
        return
    except ValueError as error:
        yield create_sse_event({"error": str(error)})
        yield "data: [DONE]\n\n"
        return

    log_event(
        logger,
        logging.INFO,
        "document_answer_queued",
        conversation_id=request.conversation_id,
        answer_job_id=answer_job["id"],
        user_message_id=user_message["id"],
        document_count=len(document_ids),
    )
    yield create_sse_event(
        {
            "answer_job": {
                "id": answer_job["id"],
                "status": answer_job["status"],
            }
        }
    )
    yield "data: [DONE]\n\n"


def get_active_documents_for_chat(request: ChatRequest) -> list[dict]:
    return list_conversation_documents_from_db(
        request.conversation_id,
        request.user_id,
    )
