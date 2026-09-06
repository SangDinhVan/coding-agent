import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import Agent
from tools.base import ToolResult
from tests.fakes import FakeTool, stream_text, stream_tool_call


class AgentLoopCharacterizationTests(unittest.TestCase):
    def agent(self, directory: str) -> Agent:
        return Agent(str(Path(directory) / "events.jsonl"), workdir=directory)

    def test_direct_text_completes_and_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                self.assertEqual(agent.run_turn("goal"), "done")
            self.assertEqual(agent.turn_state.status, "done")
            self.assertEqual([m["role"] for m in agent.event_store.to_messages()], ["user", "assistant"])

    def test_tool_result_is_sent_back_before_final_response(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            tool = FakeTool()
            responses = [stream_tool_call("call-1", "fake"), stream_text("finished")]
            with patch("agent.loop.registry.get_tool", return_value=tool), patch(
                "agent.loop.registry.get_schemas", return_value=[]
            ), patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(agent.run_turn("goal"), "finished")
            messages = agent.event_store.to_messages()
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(messages[2]["tool_call_id"], "call-1")
            self.assertEqual(messages[2]["content"], "compact-ok")

    def test_failed_tool_result_does_not_crash_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            tool = FakeTool(ToolResult("raw-error", "compact-error", False))
            responses = [stream_tool_call("call-1", "fake"), stream_text("recovered")]
            with patch("agent.loop.registry.get_tool", return_value=tool), patch(
                "agent.loop.registry.get_schemas", return_value=[]
            ), patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(agent.run_turn("goal"), "recovered")
            self.assertEqual(tool.calls, 1)

    def test_max_iterations_stops_after_exact_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            tool = FakeTool()
            with patch("agent.loop.registry.get_tool", return_value=tool), patch(
                "agent.loop.registry.get_schemas", return_value=[]
            ), patch("agent.loop.llm.complete", return_value=stream_tool_call("call-1", "fake")) as complete:
                self.assertEqual(agent.run_turn("goal", max_iterations=2), "")
            self.assertEqual(complete.call_count, 2)
            self.assertEqual(agent.turn_state.status, "failed")

    def test_interrupt_does_not_persist_partial_text_as_final(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)

            def interrupted_stream():
                yield from stream_text("partial")
                raise KeyboardInterrupt

            with patch("agent.loop.llm.complete", return_value=interrupted_stream()):
                self.assertEqual(agent.run_turn("goal"), "")
            self.assertEqual([m["role"] for m in agent.event_store.to_messages()], ["user"])


if __name__ == "__main__":
    unittest.main()
