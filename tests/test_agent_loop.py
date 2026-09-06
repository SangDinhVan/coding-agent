import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import Agent
from tools.base import ToolResult
from tests.fakes import FakeTool, stream_text, stream_tool_call


class AgentLoopCharacterizationTests(unittest.TestCase):
    def setUp(self):
        memory_update = patch.object(Agent, "_update_memory_bg", return_value=None)
        memory_update.start()
        self.addCleanup(memory_update.stop)

    def agent(self, directory: str) -> Agent:
        agent = Agent(str(Path(directory) / "events.jsonl"), workdir=directory)
        self.addCleanup(agent.event_store.close)
        return agent

    def test_direct_text_completes_and_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                self.assertEqual(agent.run_turn("goal"), "done")
            self.assertEqual(agent.turn_state.status.value, "completed")
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
            self.assertEqual(agent.turn_state.status.value, "failed")

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

class DurableTurnLifecycleTests(AgentLoopCharacterizationTests):
    def event_types(self, agent):
        return [e["event_type"] for e in agent.event_store.read_all()]

    def test_direct_text_records_started_final_and_completed_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                agent.run_turn("goal")
            self.assertEqual(self.event_types(agent), [
                "UserMessageRecorded", "TurnStarted", "TurnIterationAdvanced",
                "CompletionRequested", "AssistantMessageRecorded", "TurnCompleted",
            ])

    def test_iteration_is_durable_and_max_limit_has_structured_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            tool = FakeTool()
            with patch("agent.loop.registry.get_tool", return_value=tool), patch(
                "agent.loop.registry.get_schemas", return_value=[]
            ), patch("agent.loop.llm.complete", return_value=stream_tool_call("c", "fake")):
                agent.run_turn("goal", max_iterations=2)
            self.assertEqual(agent.turn_state.iteration, 2)
            self.assertEqual(agent.turn_state.error.category, "max_iterations")

    def test_model_exception_records_failed_and_remains_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    agent.run_turn("goal")
            self.assertEqual(agent.turn_state.status.value, "failed")
            self.assertEqual(self.event_types(agent)[-1], "TurnFailed")

    def test_stream_interrupt_records_interrupted_without_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            def interrupted():
                yield from stream_text("partial")
                raise KeyboardInterrupt
            with patch("agent.loop.llm.complete", return_value=interrupted()):
                agent.run_turn("goal")
            self.assertEqual(agent.turn_state.status.value, "interrupted")
            self.assertNotIn("AssistantMessageRecorded", self.event_types(agent))
