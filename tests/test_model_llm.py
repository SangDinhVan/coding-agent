import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from model import llm


class LlmConfigurationTests(unittest.TestCase):
    def test_complete_sets_finite_timeout_and_bounded_sdk_retries(self):
        client = Mock()
        client.chat.completions.create.return_value = "response"
        with patch("model.llm.OpenAI", return_value=client) as openai:
            self.assertEqual(llm.complete([{"role": "user", "content": "hi"}]), "response")
        self.assertEqual(openai.call_args.kwargs["timeout"], 120)
        self.assertEqual(openai.call_args.kwargs["max_retries"], 2)
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
            max_retries=2,
        )
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["tools"], tools)
        self.assertEqual(request["model"], "demo-model")
        self.assertTrue(request["stream"])
        self.assertEqual(request["stream_options"], {"include_usage": True})

    def test_timeout_before_response_retries_same_request(self):
        import httpx
        from openai import OpenAI
        attempts = []

        def respond(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            return httpx.Response(200, json={
                'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': 'done'}}],
            })

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with patch('model.llm.OpenAI', side_effect=lambda **kw: OpenAI(**kw, http_client=client)), patch('openai._base_client.time.sleep'):
            response = llm.complete([{'role': 'user', 'content': 'synthetic prompt'}],
                                    api_key='test', base_url='https://example.test/v1')
        self.assertEqual(response.choices[0].message.content, 'done')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])

    def test_timeout_after_stream_starts_does_not_replay_response(self):
        import httpx
        from openai import OpenAI
        attempts = []

        class InterruptedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"id":"test","object":"chat.completion.chunk","created":0,"model":"test","choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadTimeout('synthetic stream timeout')

        def respond(request):
            attempts.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=InterruptedStream())

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with patch('model.llm.OpenAI', side_effect=lambda **kw: OpenAI(**kw, http_client=client)):
            response = llm.complete([{'role':'user','content':'synthetic prompt'}],
                                    api_key='test', base_url='https://example.test/v1', stream=True)
            self.assertEqual(next(response).choices[0].delta.content, 'partial')
            with self.assertRaises(httpx.ReadTimeout):
                list(response)
        self.assertEqual(len(attempts), 1)


class LlmTraceTests(unittest.TestCase):
    def test_trace_refuses_symlink_parents_and_existing_linked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'outside'; outside.mkdir()
            (root / 'link').symlink_to(outside, target_is_directory=True)
            target = outside / 'target.txt'; target.write_text('keep')
            (root / 'trace.txt').symlink_to(target)
            for path in (root / 'link' / 'trace.txt', root / 'trace.txt'):
                with self.subTest(path=path), self.assertRaises(OSError):
                    llm.start_trace(path)
                llm.finish_trace()
            self.assertEqual(target.read_text(), 'keep')
            self.assertFalse((outside / 'trace.txt').exists())

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
