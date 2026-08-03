from ..infrastructure.database import insert_message_db, enqueue_summary_job_from_db
from ..infrastructure.document_job_repository import save_and_complete_answer_job_db
from ..infrastructure.document_node_repository import save_answer_sources_db
from ..infrastructure.document_repository import link_message_to_documents_db
from ..workflows.document_answer import document_answer_graph


def generate_document_answer(user_id: int, conversation_id: int, question: str, documents: list[dict]) -> tuple[str, list[dict], str]:
    initial_graph_state = {"user_id": user_id, "conversation_id": conversation_id, "question": question, "documents": documents}
    result = document_answer_graph.invoke(initial_graph_state)
    return result["validated_answer"], result["retrieved_sources"], result["answer_depth"]


def generate_and_save_document_answer(user_id: int, conversation_id: int, question: str, documents: list[dict], answer_job_id: int | None = None) -> dict:
    answer, retrieved_sources, answer_depth = generate_document_answer(user_id, conversation_id, question, documents)
    document_ids = [document["id"] for document in documents]
    source_ids = [source["id"] for source in retrieved_sources]

    if answer_job_id is not None:
        assistant_message = save_and_complete_answer_job_db(answer_job_id, user_id, conversation_id, answer, answer_depth, document_ids, source_ids)

        if not assistant_message:
            raise RuntimeError("Answer job is no longer claimable")
    else:
        assistant_message = insert_message_db(user_id, conversation_id, "assistant", answer)
        link_message_to_documents_db(assistant_message["id"], document_ids)
        save_answer_sources_db(assistant_message["id"], source_ids)

    enqueue_summary_job_from_db(conversation_id, assistant_message["id"])

    return assistant_message
