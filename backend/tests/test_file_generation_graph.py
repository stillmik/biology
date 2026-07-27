import unittest

from unittest.mock import patch

from app.workflows.graph.nodes import file_generation_node


class FileGenerationGraphTests(unittest.TestCase):
    @patch("app.workflows.graph.nodes.generate_model_response", return_value="Expanded document content")
    def test_file_generation_uses_dedicated_prompt_and_token_budget(self, generate_model_response):
        state = {"history": [{"role": "system", "content": "chat prompt"}, {"role": "user", "content": "Explain bacterial DNA"}]}

        result = file_generation_node(state)

        self.assertEqual(result["file_content"], "Expanded document content")
        model_input = generate_model_response.call_args.args[0]
        self.assertIn("expanded standalone educational document", model_input[0]["content"])
        self.assertEqual(model_input[-1]["content"], "Explain bacterial DNA")
        self.assertEqual(generate_model_response.call_args.kwargs["operation"], "file_generation")


if __name__ == "__main__":
    unittest.main()
