from ..infrastructure.database import create_message_from_db, enqueue_summary_job_from_db
from ..infrastructure.document_repository import (
    link_message_to_documents_from_db,
    save_and_complete_answer_job_from_db,
    save_answer_evidence_from_db,
)
from ..workflows.document_answer import document_answer_graph


def generate_document_answer(
    user_id: int,
    conversation_id: int,
    question: str,
    documents: list[dict],
) -> tuple[str, list[dict]]:
    result = document_answer_graph.invoke(
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "question": question,
            "documents": documents,
        }
    )
    return result["validated_answer"], result["evidence_nodes"]


def generate_and_save_document_answer(
    user_id: int,
    conversation_id: int,
    question: str,
    documents: list[dict],
    answer_job_id: int | None = None,
) -> dict:
    answer, evidence_nodes = generate_document_answer(
        user_id,
        conversation_id,
        question,
        documents,
    )
    document_ids = [document["id"] for document in documents]
    evidence_node_ids = [node["id"] for node in evidence_nodes]

    if answer_job_id is not None:
        assistant_message = save_and_complete_answer_job_from_db(
            answer_job_id,
            user_id,
            conversation_id,
            answer,
            document_ids,
            evidence_node_ids,
        )

        if not assistant_message:
            raise RuntimeError("Answer job is no longer claimable")
    else:
        assistant_message = create_message_from_db(
            user_id,
            conversation_id,
            "assistant",
            answer,
        )
        link_message_to_documents_from_db(assistant_message["id"], document_ids)
        save_answer_evidence_from_db(assistant_message["id"], evidence_node_ids)

    enqueue_summary_job_from_db(conversation_id, assistant_message["id"])

    return assistant_message
