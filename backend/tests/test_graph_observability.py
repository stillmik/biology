import unittest

from unittest.mock import patch

from app.graph import prepare_graph
from app.helper_functions import create_initial_graph_state


class GraphObservabilityTests(unittest.TestCase):
    def test_no_summary_branch_keeps_all_raw_messages(self):
        rows = [{"id": 1, "role": "user", "content": "alpha"}, {"id": 2, "role": "assistant", "content": "beta"}, {"id": 3, "role": "user", "content": "gamma"}]
        with patch("app.graph.context.get_latest_summary_segment_from_db", return_value=None), patch("app.graph.context.list_recent_summary_segments_within_token_budget_from_db", return_value=[]), patch("app.graph.context.list_conversation_messages_after_from_db", return_value=rows), patch("app.graph.context.SUMMARY_TRIGGER_TOKENS", 10000):
            result = prepare_graph.invoke(create_initial_graph_state(7), config={"configurable": {"thread_id": "7"}})
        self.assertEqual(result["summary_decision"], "build_context")
        self.assertEqual(result["summary_reason"], "unsummarized_messages_below_trigger_threshold")
        self.assertEqual([message["content"] for message in result["history"][1:]], ["alpha", "beta", "gamma"])

    def test_summary_segment_is_non_overlapping_and_raw_tail_is_preserved(self):
        rows = [{"id": 1, "role": "user", "content": "a" * 220}, {"id": 2, "role": "assistant", "content": "b" * 220}, {"id": 3, "role": "user", "content": "c" * 220}, {"id": 4, "role": "assistant", "content": "latest"}]
        latest_segment = {"id": 20, "content": "segment one", "token_count": 3, "covered_from_message_id": 1, "covered_until_message_id": 3}
        attached_segments = [latest_segment]
        with patch("app.graph.context.get_latest_summary_segment_from_db", side_effect=[None, latest_segment]), patch("app.graph.context.list_recent_summary_segments_within_token_budget_from_db", side_effect=[[], attached_segments]), patch("app.graph.context.list_conversation_messages_after_from_db", side_effect=[rows, [{"id": 4, "role": "assistant", "content": "latest"}]]), patch("app.graph.nodes.create_summary_segment_from_db") as create_segment, patch("app.graph.nodes.generate_model_response", return_value="segment one"), patch("app.graph.context.SUMMARY_TRIGGER_TOKENS", 100), patch("app.graph.context.KEEP_RECENT_TOKENS", 30), patch("app.graph.context.SUMMARY_CHUNK_MAX_TOKENS", 1000):
            result = prepare_graph.invoke(create_initial_graph_state(8), config={"configurable": {"thread_id": "8"}})
        create_segment.assert_called_once_with(8, "segment one", 3, 1, 3)
        self.assertEqual(result["summary_passes"], 1)
        self.assertEqual(result["summary_cursor"], 3)
        self.assertEqual(result["included_summary"]["segment_count"], 1)
        self.assertIn("segment one", result["history"][1]["content"])
        self.assertEqual(result["history"][-1]["content"], "latest")

    def test_context_attaches_newest_segments_that_fit_budget_in_chronological_order(self):
        segments = [{"id": 1, "content": "old", "token_count": 330, "covered_from_message_id": 1, "covered_until_message_id": 4}, {"id": 2, "content": "middle", "token_count": 330, "covered_from_message_id": 5, "covered_until_message_id": 7}, {"id": 3, "content": "new", "token_count": 330, "covered_from_message_id": 8, "covered_until_message_id": 11}]
        with patch("app.graph.context.get_latest_summary_segment_from_db", return_value=segments[-1]), patch("app.graph.context.list_recent_summary_segments_within_token_budget_from_db", return_value=segments), patch("app.graph.context.list_conversation_messages_after_from_db", return_value=[{"id": 12, "role": "user", "content": "raw tail"}]):
            result = prepare_graph.invoke(create_initial_graph_state(9), config={"configurable": {"thread_id": "9"}})
        self.assertEqual(result["included_summary"]["segment_count"], 3)
        self.assertEqual([message["content"] for message in result["history"][1:4]], ["Conversation summary (messages 1-4):\n\nold", "Conversation summary (messages 5-7):\n\nmiddle", "Conversation summary (messages 8-11):\n\nnew"])
        self.assertEqual(result["history"][-1]["content"], "raw tail")


if __name__ == "__main__":
    unittest.main()
