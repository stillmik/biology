import logging

from ..db import create_message_from_db, lock_conversation_from_db, count_messages_after_from_db, update_user_message_and_delete_following_from_db
from ..graph import chat_graph
from ..helper_functions import create_initial_graph_state
from ..observability import chatbot_trace, finish_chatbot_trace, hash_identifier, langgraph_config, log_event, observe_graph_execution, observe_operation
from ..db import get_user_message_from_db

logger = logging.getLogger(__name__)


def generate_chat_reply(user_id: int, conversation_id: int, message: str) -> str:
    with observe_operation("chat"), lock_conversation_from_db(conversation_id):

        with chatbot_trace(user_id, conversation_id, "chat_response", message) as trace_run:
            create_message_from_db(user_id, conversation_id, "user", message)
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="user")

            with observe_graph_execution("chat_response"):
                result = chat_graph.invoke(create_initial_graph_state(conversation_id), config=langgraph_config(user_id, conversation_id, "chat_response"))

            reply = result["reply"]
            create_message_from_db(user_id, conversation_id, "assistant", reply)
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")
            finish_chatbot_trace(trace_run, {"reply": reply, "summary_decision": result["summary_decision"], "summary_reason": result["summary_reason"], "included_summary": result["included_summary"], "summary_passes": result["summary_passes"], "summary_cursor": result["summary_cursor"], "unsummarized_message_count": len(result["unsummarized_messages"]), "projected_tokens": result["projected_tokens"], "tokens_until_summarization": result["tokens_until_summarization"], "summarization_trigger_progress": result["summarization_trigger_progress"]})
    return reply


def regenerate_chat_reply(message_id: int, user_id: int, content: str) -> str:
    message = get_user_message_from_db(message_id, user_id)

    if not message:
        return ""

    conversation_id = message["conversation_id"]
    with observe_operation("regenerate_message"), lock_conversation_from_db(conversation_id):
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
            create_message_from_db(user_id, conversation_id, "assistant", reply)
            log_event(logger, logging.INFO, "message_saved", conversation_id=conversation_id, user_id_hash=hash_identifier(user_id), role="assistant")
            finish_chatbot_trace(trace_run, {"reply": reply, "edited_message_id": message_id, "messages_deleted": messages_deleted, "summary_decision": result["summary_decision"], "summary_reason": result["summary_reason"], "included_summary": result["included_summary"], "summary_passes": result["summary_passes"], "summary_cursor": result["summary_cursor"], "projected_tokens": result["projected_tokens"], "tokens_until_summarization": result["tokens_until_summarization"], "summarization_trigger_progress": result["summarization_trigger_progress"]})
    return reply
