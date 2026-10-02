import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.assertEqual(request["stream_options"], {"include_usage": True})


class LlmTraceTests(unittest.TestCase):
    def response(self, *, prompt_tokens=11, completion_tokens=7):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    def test_non_stream_call_writes_provider_tokens_and_exact_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            client = Mock()
            client.chat.completions.create.return_value = self.response()
            llm.start_trace(path)
            self.addCleanup(llm.finish_trace)

            with patch("model.llm.OpenAI", return_value=client):
                llm.complete(
                    [{"role": "user", "content": "hello"}],
                    tools=[{"type": "function", "function": {"name": "demo"}}],
                )
            llm.finish_trace()

            trace = path.read_text(encoding="utf-8")
            self.assertIn("lần gọi thứ: 1", trace)
            self.assertIn("token input: 11 (provider)", trace)
            self.assertIn("token output: 7 (provider)", trace)
            self.assertIn('"content": "hello"', trace)
            self.assertIn('"name": "demo"', trace)

    def test_stream_call_records_usage_without_consuming_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="done", tool_calls=[]))],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=13, completion_tokens=5),
                ),
            ]
            client = Mock()
            client.chat.completions.create.return_value = iter(chunks)
            llm.start_trace(path)
            self.addCleanup(llm.finish_trace)

            with patch("model.llm.OpenAI", return_value=client):
                received = list(llm.complete([{"role": "user", "content": "stream"}], stream=True))
            llm.finish_trace()

            self.assertEqual(received, chunks)
            trace = path.read_text(encoding="utf-8")
            self.assertIn("token input: 13 (provider)", trace)
            self.assertIn("token output: 5 (provider)", trace)

    def test_reopening_trace_continues_call_numbering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            client = Mock()
            client.chat.completions.create.return_value = self.response()

            with patch("model.llm.OpenAI", return_value=client):
                llm.start_trace(path)
                llm.complete([{"role": "user", "content": "first"}])
                llm.finish_trace()
                llm.start_trace(path)
                llm.complete([{"role": "user", "content": "second"}])
                llm.finish_trace()

            trace = path.read_text(encoding="utf-8")
            self.assertEqual(trace.count("lần gọi thứ:"), 2)
            self.assertIn("lần gọi thứ: 2", trace)


if __name__ == "__main__":
    unittest.main()
