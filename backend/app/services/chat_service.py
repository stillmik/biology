import logging

from ..core.observability import chatbot_trace, finish_chatbot_trace, get_chatbot_trace_id, hash_identifier, langgraph_config, log_event, observe_graph_execution, observe_operation
from ..infrastructure.database import count_messages_after_from_db, insert_message_db, enqueue_summary_job_from_db, get_user_message_from_db, lock_conversation_in_db, update_user_message_and_delete_following_from_db
from ..utils.chat_context import create_initial_graph_state
from ..workflows.graph import chat_graph

logger = logging.getLogger(__name__)


def generate_chat_reply(user_id: int, conversation_id: int, message: str) -> str:
    with observe_operation("chat"), lock_conversation_in_db(conversation_id):

        with chatbot_trace(user_id, conversation_id, "chat_response", message) as trace_run:
            user_message = insert_message_db(user_id, conversation_id, "user", message)
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="user")

            with observe_graph_execution("chat_response"):
                result = chat_graph.invoke(create_initial_graph_state(conversation_id), config=langgraph_config(user_id, conversation_id, "chat_response"))

            reply = result["reply"]
            assistant_message = insert_message_db(user_id, conversation_id, "assistant", reply)
            summary_job = enqueue_summary_job_from_db(conversation_id, assistant_message["id"], get_chatbot_trace_id(trace_run))
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")
            finish_chatbot_trace(trace_run, {"reply": reply, "source_message_id": user_message["id"], "summary_job_id": summary_job["id"], "summary_decision": result["summary_decision"], "summary_reason": result["summary_reason"], "included_summary": result["included_summary"], "summary_passes": result["summary_passes"], "summary_cursor": result["summary_cursor"], "unsummarized_message_count": len(result["unsummarized_messages"]), "raw_message_tokens": result["raw_message_tokens"], "projected_tokens": result["projected_tokens"], "tokens_until_summarization": result["tokens_until_summarization"], "summarization_trigger_progress": result["summarization_trigger_progress"]})
    return reply


def regenerate_chat_reply(message_id: int, user_id: int, content: str) -> str:
    message = get_user_message_from_db(message_id, user_id)

    if not message:
        return ""

    conversation_id = message["conversation_id"]
    with observe_operation("regenerate_message"), lock_conversation_in_db(conversation_id):
        messages_deleted = count_messages_after_from_db(conversation_id, message_id)
        trace_metadata = {"edited_message_id": message_id, "messages_deleted": messages_deleted}

        with chatbot_trace(user_id, conversation_id, "message_regeneration", content, trace_metadata) as trace_run:
            updated_message = update_user_message_and_delete_following_from_db(message_id=message_id, user_id=user_id, new_content=content)

            if not updated_message:
                return ""

            log_event(logger, logging.INFO, "message_edited", conversation_id=conversation_id, message_id=message_id, user_id_hash=hash_identifier(user_id), messages_deleted=messages_deleted)
            graph_config = langgraph_config(user_id, conversation_id, "message_regeneration")
            graph_config["metadata"].update(trace_metadata)

            with observe_graph_execution("message_regeneration"):
                result = chat_graph.invoke(create_initial_graph_state(conversation_id), config=graph_config)

            reply = result["reply"]
            assistant_message = insert_message_db(user_id, conversation_id, "assistant", reply)
            summary_job = enqueue_summary_job_from_db(conversation_id, assistant_message["id"], get_chatbot_trace_id(trace_run))
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")
            finish_chatbot_trace(trace_run, {"reply": reply, "edited_message_id": message_id, "messages_deleted": messages_deleted, "summary_job_id": summary_job["id"], "summary_decision": result["summary_decision"], "summary_reason": result["summary_reason"], "included_summary": result["included_summary"], "summary_passes": result["summary_passes"], "summary_cursor": result["summary_cursor"], "raw_message_tokens": result["raw_message_tokens"], "projected_tokens": result["projected_tokens"], "tokens_until_summarization": result["tokens_until_summarization"], "summarization_trigger_progress": result["summarization_trigger_progress"]})
    return reply
