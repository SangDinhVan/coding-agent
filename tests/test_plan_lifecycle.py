import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import Agent
from runtime.models import PlanMode, PlanStepStatus
from tests.fakes import stream_text, stream_tool_call


class PlanLifecycleTests(unittest.TestCase):
    def agent(self, directory):
        agent = Agent(str(Path(directory) / "events.jsonl"), workdir=directory)
        self.addCleanup(agent.close)
        update = patch.object(Agent, "_update_memory_bg", return_value=None)
        update.start()
        self.addCleanup(update.stop)
        return agent

    def test_optional_without_plan_allows_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                self.assertEqual(agent.run_turn("g"), "done")

    def test_required_without_plan_blocks_and_third_attempt_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("too early")) as complete:
                self.assertEqual(agent.run_turn("g", plan_mode=PlanMode.REQUIRED), "")
            self.assertEqual(complete.call_count, 3)
            self.assertEqual(agent.turn_state.error.category, "repeated_incomplete_plan")
            messages = agent.event_store.to_messages()
            self.assertFalse(any(m.get("content") == "too early" for m in messages))

    def test_structured_plan_can_complete_required_work(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call("p1", "update_plan", '{"action":"create","steps":[{"task":"work"}]}'),
                stream_tool_call("p2", "update_plan", '{"action":"set_step_status","step_id":"step_1","status":"in_progress"}'),
                stream_tool_call("p3", "update_plan", '{"action":"set_step_status","step_id":"step_1","status":"completed","note":"done"}'),
                stream_text("finished"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(agent.run_turn("g", plan_mode=PlanMode.REQUIRED), "finished")
            plan = agent.runtime_state.plans[agent.runtime_state.active_plan_id]
            self.assertEqual(plan.revisions[-1].steps[0].status, PlanStepStatus.COMPLETED)

    def test_pending_to_completed_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call("p1", "update_plan", '{"action":"create","steps":[{"task":"work"}]}'),
                stream_tool_call("p2", "update_plan", '{"action":"set_step_status","step_id":"step_1","status":"completed","note":"done"}'),
                stream_text("still blocked"), stream_text("still blocked"), stream_text("still blocked"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses):
                agent.run_turn("g", plan_mode=PlanMode.REQUIRED)
            failed = [e for e in agent.event_store.read_all() if e.get("event_type") == "ToolFailed"]
            self.assertIn("invalid plan step transition", failed[0]["payload"]["error"]["message"])

    def test_evidence_requires_successful_same_session_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call("p1", "update_plan", '{"action":"create","steps":[{"task":"work","completion_policy":"evidence_required"}]}'),
                stream_tool_call("p2", "update_plan", '{"action":"set_step_status","step_id":"step_1","status":"in_progress"}'),
                stream_tool_call("p3", "update_plan", '{"action":"set_step_status","step_id":"step_1","status":"completed","evidence_execution_ids":["missing"]}'),
                stream_text("x"), stream_text("x"), stream_text("x"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses):
                agent.run_turn("g", plan_mode=PlanMode.REQUIRED)
            failed = [e for e in agent.event_store.read_all() if e.get("event_type") == "ToolFailed"]
            self.assertIn("evidence", failed[0]["payload"]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
