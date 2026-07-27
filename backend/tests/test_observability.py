import json
import logging
import unittest

from app.core.observability import JsonLogFormatter, anonymize_trace_data, hash_identifier, langgraph_config, metrics_response


class ObservabilityTests(unittest.TestCase):
    def test_json_formatter_redacts_sensitive_extra_fields(self):
        record = logging.makeLogRecord({"name": "test", "levelno": logging.INFO, "levelname": "INFO", "msg": "test_event", "args": (), "api_key": "secret-value", "conversation_id": 7})
        payload = json.loads(JsonLogFormatter().format(record))
        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertEqual(payload["conversation_id"], 7)
        self.assertEqual(payload["event"], "test_event")

    def test_identifier_hash_is_stable_and_not_raw(self):
        first = hash_identifier(42)
        second = hash_identifier(42)
        self.assertEqual(first, second)
        self.assertNotEqual(first, "42")
        self.assertEqual(len(first), 16)

    def test_trace_anonymizer_redacts_credentials_but_preserves_prompt(self):
        trace_data = {"input": "Explain bacteria and use xai-\n123456789012345678901234567890", "database": "postgresql://biology:password@db:5432/biology"}
        anonymized = anonymize_trace_data(trace_data)
        self.assertEqual(anonymized["input"], "Explain bacteria and use [REDACTED_XAI_KEY]")
        self.assertEqual(anonymized["database"], "[REDACTED_DATABASE_URL]")

    def test_json_formatter_redacts_secrets_inside_exception_text(self):
        try:
            raise RuntimeError("Failed with postgresql://biology:password@db:5432/biology")
        except RuntimeError:
            record = logging.makeLogRecord({"name": "test", "levelno": logging.ERROR, "levelname": "ERROR", "msg": "failed", "args": (), "exc_info": __import__("sys").exc_info()})
        payload = json.loads(JsonLogFormatter().format(record))
        self.assertNotIn("password", payload["stack_trace"])
        self.assertIn("[REDACTED_DATABASE_URL]", payload["stack_trace"])

    def test_langgraph_config_groups_runs_by_conversation(self):
        config = langgraph_config(user_id=10, conversation_id=20, execution_type="chat_response")
        self.assertEqual(config["metadata"]["thread_id"], "20")
        self.assertEqual(config["metadata"]["conversation_id"], "20")
        self.assertEqual(config["configurable"]["thread_id"], "20")
        self.assertNotEqual(config["metadata"]["user_id_hash"], "10")

    def test_metrics_endpoint_contains_application_metrics(self):
        response = metrics_response()
        body = bytes(response.body).decode("utf-8")
        self.assertIn("biology_http_requests_total", body)
        self.assertIn("biology_langgraph_executions_total", body)
        self.assertIn("biology_langsmith_traces_attempted_total", body)


if __name__ == "__main__":
    unittest.main()
