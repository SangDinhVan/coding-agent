import unittest
from unittest.mock import Mock, patch

from model import llm


class LlmConfigurationTests(unittest.TestCase):
    def test_complete_sets_finite_timeout_without_optional_retry_wrapper(self):
        client = Mock()
        client.chat.completions.create.return_value = "response"
        with patch("model.llm.OpenAI", return_value=client) as openai:
            self.assertEqual(llm.complete([{"role": "user", "content": "hi"}]), "response")
        self.assertEqual(openai.call_args.kwargs["timeout"], 120)
        self.assertEqual(openai.call_args.kwargs["max_retries"], 0)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], llm.MODEL)
        self.assertEqual(request["messages"], [{"role": "user", "content": "hi"}])
        self.assertFalse(request["stream"])
        self.assertNotIn("tools", request)

    def test_complete_preserves_explicit_timeout_and_retries(self):
        client = Mock()
        with patch("model.llm.OpenAI", return_value=client) as openai:
            llm.complete([], timeout=15, max_retries=0)
        self.assertEqual(openai.call_args.kwargs["timeout"], 15)
        self.assertEqual(openai.call_args.kwargs["max_retries"], 0)

    def test_complete_passes_tools_and_connection_overrides(self):
        tools = [{"type": "function", "function": {"name": "demo"}}]
        client = Mock()
        with patch("model.llm.OpenAI", return_value=client) as openai:
            llm.complete(
                [],
                tools=tools,
                model="demo-model",
                base_url="https://example.test/v1",
                api_key="test-key",
                stream=True,
            )
        openai.assert_called_once_with(
            api_key="test-key",
            base_url="https://example.test/v1",
            timeout=120,
            max_retries=0,
        )
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["tools"], tools)
        self.assertEqual(request["model"], "demo-model")
        self.assertTrue(request["stream"])


if __name__ == "__main__":
    unittest.main()
