import unittest

from unittest.mock import patch

from app.graph import prepare_graph
from app.helper_functions import create_initial_graph_state


class GraphObservabilityTests(unittest.TestCase):
    def test_no_summary_branch_keeps_every_unsummarized_message(self):
        rows = [{"id": 1, "role": "user", "content": "alpha"}, {"id": 2, "role": "assistant", "content": "beta"}, {"id": 3, "role": "user", "content": "gamma"}]
        with patch("app.graph.context.get_latest_summary_from_db", return_value=None), patch("app.graph.context.list_conversation_messages_after_from_db", return_value=rows), patch("app.graph.context.SUMMARY_TRIGGER_TOKENS", 10000):
            result = prepare_graph.invoke(create_initial_graph_state(7), config={"configurable": {"thread_id": "7"}})
        self.assertEqual(result["summary_decision"], "build_context")
        self.assertEqual(result["summary_reason"], "below_trigger_threshold")
        self.assertEqual([message["content"] for message in result["history"][1:]], ["alpha", "beta", "gamma"])

    def test_summary_branch_reloads_summary_and_remaining_messages(self):
        rows = [{"id": 1, "role": "user", "content": "a" * 220}, {"id": 2, "role": "assistant", "content": "b" * 220}, {"id": 3, "role": "user", "content": "c" * 220}, {"id": 4, "role": "assistant", "content": "latest"}]
        latest_row = [{"id": 4, "role": "assistant", "content": "latest"}]
        summaries = [None, {"content": "rolling summary", "covered_until_message_id": 3}]
        with patch("app.graph.context.get_latest_summary_from_db", side_effect=summaries), patch("app.graph.context.list_conversation_messages_after_from_db", side_effect=[rows, latest_row]), patch("app.graph.nodes.create_summary_from_db") as create_summary, patch("app.graph.nodes.generate_model_response", return_value="rolling summary"), patch("app.graph.context.SUMMARY_TRIGGER_TOKENS", 300), patch("app.graph.context.KEEP_RECENT_TOKENS", 30), patch("app.graph.context.SUMMARY_CHUNK_MAX_TOKENS", 1000):
            result = prepare_graph.invoke(create_initial_graph_state(8), config={"configurable": {"thread_id": "8"}})
        create_summary.assert_called_once()
        self.assertEqual(result["summary_passes"], 1)
        self.assertEqual(result["summary_cursor"], 3)
        self.assertEqual(result["summary_decision"], "build_context")
        self.assertIn("rolling summary", result["history"][1]["content"])
        self.assertEqual(result["history"][-1]["content"], "latest")


if __name__ == "__main__":
    unittest.main()
