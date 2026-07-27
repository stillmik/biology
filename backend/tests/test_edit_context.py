import unittest

from unittest.mock import patch

from app.infrastructure.database import invalidate_memories_after_message_from_db
from app.utils.chat_context import create_initial_graph_state
from app.workflows.graph import prepare_graph


class DatabaseResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class RecordingDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query: str, parameters: tuple) -> DatabaseResult:
        self.calls.append((query, parameters))
        return DatabaseResult(len(self.calls))


class EditContextTests(unittest.TestCase):
    def test_edit_invalidation_removes_only_memories_reaching_the_edited_message(self):
        database = RecordingDatabase()

        result = invalidate_memories_after_message_from_db(database, conversation_id=7, message_id=10)

        self.assertEqual(result, {"segments_deleted": 1, "summaries_deleted": 2, "jobs_cancelled": 3})
        self.assertIn("conversation_summary_segments", database.calls[0][0])
        self.assertIn("covered_until_message_id >= %s", database.calls[0][0])
        self.assertEqual(database.calls[0][1], (7, 10))
        self.assertIn("conversation_summaries", database.calls[1][0])
        self.assertIn("summary_jobs", database.calls[2][0])

    def test_regeneration_context_uses_valid_summary_before_edit_and_raw_messages_through_edit(self):
        valid_summary = {"id": 1, "content": "Messages one through four", "token_count": 8, "covered_from_message_id": 1, "covered_until_message_id": 4}
        raw_messages = [{"id": 5, "role": "user", "content": "Original question"}, {"id": 6, "role": "assistant", "content": "Original answer"}, {"id": 7, "role": "user", "content": "Edited question"}]

        with patch("app.workflows.graph.context.get_latest_summary_segment_from_db", return_value=valid_summary), patch("app.workflows.graph.context.list_recent_summary_segments_within_token_budget_from_db", return_value=[valid_summary]), patch("app.workflows.graph.context.list_conversation_messages_after_from_db", return_value=raw_messages):
            result = prepare_graph.invoke(create_initial_graph_state(7), config={"configurable": {"thread_id": "7"}})

        self.assertEqual(result["summary_cursor"], 4)
        self.assertEqual([message["content"] for message in result["history"][1:]], ["Conversation summary (messages 1-4):\n\nMessages one through four", "Original question", "Original answer", "Edited question"])


if __name__ == "__main__":
    unittest.main()
