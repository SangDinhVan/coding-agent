from tests.fakes import trusted_agent
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import Agent, StreamedToolCall, tool_message_content
from runtime.models import PlanMode, PlanStepStatus
from tests.fakes import FakeTool, stream_text, stream_tool_call
from tools.plan import UpdatePlanTool


class FakeRegistry:
    def __init__(self, tool=None):
        self.tool = tool

    def schemas(self):
        return []

    def get(self, name):
        return self.tool if self.tool is not None and name == self.tool.name else None


class PlanTestCase(unittest.TestCase):
    def agent(self, directory, tool=None):
        agent = trusted_agent(
            str(Path(directory) / "events.jsonl"),
            workdir=directory,
            tool_registry=FakeRegistry(tool),
        )
        self.addCleanup(agent.close)
        return agent

    def start_turn(self, agent, turn_id="t", mode="optional"):
        agent.event_store.append("user", "g", turn_id=turn_id)
        agent._event(
            "TurnStarted",
            "turn",
            turn_id,
            {"goal": "g", "plan_mode": mode},
            turn_id=turn_id,
        )
        return turn_id

    def create_plan(self, agent, turn_id, steps):
        return agent._apply_plan_action(
            execution_id="create",
            turn_id=turn_id,
            action="create_plan",
            steps=steps,
        )

    def execute_calls(self, agent, turn_id, calls):
        agent.event_store.append(
            "assistant",
            None,
            tool_calls=[call.model_dump() for call in calls],
            turn_id=turn_id,
        )
        execution_ids = agent.tool_executor.request_batch(calls, turn_id)
        agent._refresh()
        results = []
        for call, execution_id in zip(calls, execution_ids):
            result = agent.tool_executor.execute(execution_id)
            agent._refresh()
            agent.event_store.append(
                "tool",
                tool_message_content(result, agent._execution_alias(execution_id)),
                tool_call_id=call.id,
                turn_id=turn_id,
            )
            agent._refresh()
            results.append(result)
        return results

    @staticmethod
    def call(call_id, name, arguments):
        call = StreamedToolCall()
        call.id = call_id
        call.function.name = name
        call.function.arguments = json.dumps(arguments)
        return call

    @staticmethod
    def envelope(result):
        return json.loads(result.compact)

    @staticmethod
    def state_frame(messages):
        content = messages[0]["content"]
        payload = content.split("--- Current runtime state ---\n", 1)[1]
        payload = payload.split("\n\n--- Workspace ---", 1)[0]
        return json.loads(payload)


class PlanLifecycleTests(PlanTestCase):
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
            self.assertFalse(any(
                message.get("content") == "too early"
                for message in agent.event_store.to_messages()
            ))

    def test_structured_plan_can_complete_required_work(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call(
                    "p1",
                    "update_plan",
                    '{"action":"create_plan","steps":[{"task":"work"}]}',
                ),
                stream_tool_call(
                    "p2",
                    "update_plan",
                    '{"action":"complete_step","step_id":"step_1","note":"done"}',
                ),
                stream_text("finished"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(
                    agent.run_turn("g", plan_mode=PlanMode.REQUIRED),
                    "finished",
                )
            plan = agent.runtime_state.plans[agent.runtime_state.active_plan_id]
            self.assertEqual(
                plan.revisions[-1].steps[0].status,
                PlanStepStatus.COMPLETED,
            )

    def test_rejected_completion_tells_model_which_steps_need_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = iter([
                stream_tool_call(
                    "p1", "update_plan",
                    '{"action":"create_plan","steps":[{"task":"work"}]}',
                ),
                stream_text("too early"),
                stream_tool_call(
                    "p2", "update_plan",
                    '{"action":"complete_step","step_id":"step_1","note":"done"}',
                ),
                stream_text("finished"),
            ])
            frames = []

            def complete(**kwargs):
                frame = self.state_frame(kwargs["messages"])
                frames.append(frame)
                if len(frames) == 3:
                    self.assertEqual(frame["completion_feedback"]["attempts"], 1)
                    self.assertEqual(frame["completion_feedback"]["blockers"][0]["code"], "unfinished_required_steps")
                    self.assertEqual(frame["completion_feedback"]["blockers"][0]["ids"], ["step_1"])
                return next(responses)

            with patch("agent.loop.llm.complete", side_effect=complete):
                self.assertEqual(agent.run_turn("g", plan_mode=PlanMode.REQUIRED), "finished")
            self.assertNotIn("completion_feedback", frames[-1])
            self.assertIsNone(agent.turn_state.error)

    def test_evidence_requires_successful_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call(
                    "p1",
                    "update_plan",
                    '{"action":"create_plan","steps":[{"task":"work","completion_policy":"evidence_required"}]}',
                ),
                stream_tool_call(
                    "p2",
                    "update_plan",
                    '{"action":"complete_step","step_id":"step_1","evidence_execution_ids":["missing"]}',
                ),
                stream_text("blocked"),
                stream_text("blocked"),
                stream_text("blocked"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses):
                agent.run_turn("g", plan_mode=PlanMode.REQUIRED)
            failure = next(
                event for event in agent.event_store.read_all()
                if event.get("event_type") == "ToolFailed"
            )
            self.assertIn("invalid evidence execution", failure["payload"]["error"]["message"])

    def test_enforced_final_text_is_printed_only_after_plan_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            responses = [
                stream_tool_call(
                    "p1",
                    "update_plan",
                    '{"action":"create_plan","steps":[{"task":"work"}]}',
                ),
                stream_tool_call(
                    "p2",
                    "update_plan",
                    '{"action":"complete_step","step_id":"step_1","note":"done"}',
                ),
                stream_text("accepted"),
            ]
            with patch("agent.loop.llm.complete", side_effect=responses), patch(
                "builtins.print"
            ) as output:
                self.assertEqual(
                    agent.run_turn("g", plan_mode=PlanMode.REQUIRED),
                    "accepted",
                )
            self.assertTrue(any(
                "accepted" in " ".join(str(arg) for arg in item.args)
                for item in output.call_args_list
            ))


class PlanContractTests(PlanTestCase):
    def test_create_assigns_runtime_owned_pending_step_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)

            result = self.create_plan(agent, turn_id, [
                {"step_id": "model-id", "task": "work", "status": "completed"},
            ])

            frame = self.envelope(result)["current_state"]
            self.assertTrue(result.success)
            self.assertEqual(frame["plan"]["steps"][0]["id"], "step_1")
            self.assertEqual(frame["plan"]["steps"][0]["status"], "pending")

    def test_create_rejects_non_object_without_mutating_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            before = len(agent.event_store.read_all())

            result = self.create_plan(agent, turn_id, ["bad"])

            self.assertFalse(result.success)
            self.assertEqual(self.envelope(result)["error"], "each plan step must be an object")
            self.assertEqual(len(agent.event_store.read_all()), before)

    def test_complete_step_applies_internal_start_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            self.create_plan(agent, turn_id, [{"task": "work"}])
            before = agent.runtime_state.state_version

            result = agent._apply_plan_action(
                execution_id="complete",
                turn_id=turn_id,
                action="complete_step",
                step_id="step_1",
                note="done",
            )

            envelope = self.envelope(result)
            self.assertEqual(envelope["state_version_before"], before)
            self.assertGreater(envelope["state_version_after"], before)
            self.assertEqual(
                envelope["delta"]["step_1"],
                {"from": "pending", "to": "completed"},
            )
            plan_events = [
                event["event_type"] for event in agent.event_store.read_all()
                if event.get("causation_id") == "complete"
            ]
            self.assertEqual(plan_events, ["PlanStepStarted", "PlanStepCompleted"])

    def test_repeating_completed_intent_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            self.create_plan(agent, turn_id, [{"task": "work"}])
            agent._apply_plan_action(
                execution_id="first",
                turn_id=turn_id,
                action="complete_step",
                step_id="step_1",
                note="done",
            )
            before = len(agent.event_store.read_all())

            result = agent._apply_plan_action(
                execution_id="again",
                turn_id=turn_id,
                action="complete_step",
                step_id="step_1",
                note="done",
            )

            self.assertTrue(result.success)
            self.assertEqual(self.envelope(result)["outcome"], "noop")
            self.assertEqual(len(agent.event_store.read_all()), before)

    def test_rejected_intent_returns_current_state_and_allowed_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            self.create_plan(agent, turn_id, [{"task": "work"}])

            result = agent._apply_plan_action(
                execution_id="bad",
                turn_id=turn_id,
                action="complete_step",
                step_id="missing",
                note="done",
            )

            envelope = self.envelope(result)
            self.assertFalse(result.success)
            self.assertEqual(envelope["state_version_before"], envelope["state_version_after"])
            self.assertEqual(envelope["current_state"]["plan"]["steps"][0]["id"], "step_1")
            self.assertIn(
                {"action": "complete_step", "step_id": "step_1", "requires_evidence": False},
                envelope["current_state"]["allowed_actions"],
            )

    def test_evidence_aliases_exclude_plan_control_executions(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTool()
            agent = self.agent(directory, fake)
            turn_id = self.start_turn(agent, mode="required")
            create = self.call("plan", "update_plan", {
                "action": "create_plan",
                "steps": [{"task": "verify", "completion_policy": "evidence_required"}],
            })
            work = self.call("work", "fake", {})
            self.execute_calls(agent, turn_id, [create])
            self.execute_calls(agent, turn_id, [work])

            schema = agent.plan_tool.schema(agent.runtime_state)["function"]["parameters"]
            complete = next(
                branch for branch in schema["oneOf"]
                if branch["properties"]["action"]["const"] == "complete_step"
            )

            self.assertEqual(
                complete["properties"]["evidence_execution_ids"]["items"]["enum"],
                ["exec_2"],
            )

    def test_revise_plan_patches_existing_step_without_replacing_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            self.create_plan(agent, turn_id, [{"task": "old"}])
            plan_id = agent.runtime_state.active_plan_id

            result = agent._apply_plan_action(
                execution_id="revise",
                turn_id=turn_id,
                action="revise_plan",
                reason="clarify",
                patches=[{"step_id": "step_1", "task": "new"}],
            )

            self.assertTrue(result.success)
            self.assertEqual(agent.runtime_state.active_plan_id, plan_id)
            plan = agent.runtime_state.plans[plan_id]
            self.assertEqual(plan.active_revision, 2)
            self.assertEqual(plan.revisions[-1].steps[0].task, "new")


class PlanContextSynchronizationTests(PlanTestCase):
    def test_each_iteration_uses_current_plan_state_without_replaying_control_history(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            calls = 0

            def respond(**kwargs):
                nonlocal calls
                calls += 1
                messages = kwargs["messages"]
                frame = self.state_frame(messages)
                plan_calls = [
                    tool_call
                    for message in messages
                    for tool_call in message.get("tool_calls", [])
                    if tool_call.get("function", {}).get("name") == "update_plan"
                ]
                self.assertEqual(plan_calls, [])
                if calls == 1:
                    self.assertIsNone(frame["plan"])
                    return iter(stream_tool_call(
                        "create",
                        "update_plan",
                        '{"action":"create_plan","steps":[{"task":"work"}]}',
                    ))
                if calls == 2:
                    self.assertEqual(frame["plan"]["steps"][0]["status"], "pending")
                    return iter(stream_tool_call(
                        "complete",
                        "update_plan",
                        '{"action":"complete_step","step_id":"step_1","note":"done"}',
                    ))
                self.assertEqual(frame["plan"]["steps"][0]["status"], "completed")
                return iter(stream_text("finished"))

            with patch("agent.loop.llm.complete", side_effect=respond):
                self.assertEqual(
                    agent.run_turn("g", plan_mode=PlanMode.REQUIRED),
                    "finished",
                )
            self.assertEqual(calls, 3)

    def test_reopened_agent_uses_runtime_plan_as_source_of_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            events_path = Path(directory) / "events.jsonl"
            first = trusted_agent(
                str(events_path),
                workdir=directory,
                tool_registry=FakeRegistry(),
            )
            turn_id = self.start_turn(first)
            create = self.call("create", "update_plan", {
                "action": "create_plan",
                "steps": [{"task": "work"}],
            })
            self.execute_calls(first, turn_id, [create])
            first.close()

            reopened = trusted_agent(
                str(events_path),
                workdir=directory,
                tool_registry=FakeRegistry(),
            )
            self.addCleanup(reopened.close)
            messages = reopened._build_context()
            frame = self.state_frame(messages)

            self.assertEqual(frame["plan"]["steps"][0]["status"], "pending")
            self.assertFalse(any(
                tool_call.get("function", {}).get("name") == "update_plan"
                for message in messages
                for tool_call in message.get("tool_calls", [])
            ))

    def test_latest_plan_failure_survives_unrelated_success_in_same_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeTool()
            agent = self.agent(directory, fake)
            turn_id = self.start_turn(agent)
            invalid = self.call("bad-plan", "update_plan", {
                "action": "complete_step",
                "step_id": "missing",
                "note": "done",
            })
            unrelated = self.call("other", "fake", {})

            self.execute_calls(agent, turn_id, [invalid, unrelated])
            messages = agent._build_context()

            self.assertTrue(any(
                message.get("role") == "tool"
                and message.get("tool_call_id") == "bad-plan"
                and "invalid_action" in message.get("content", "")
                for message in messages
            ))


class PlanToolContractTests(PlanTestCase):
    def test_schema_only_exposes_create_before_plan_exists(self):
        schema = UpdatePlanTool(lambda **kwargs: None).schema()["function"]["parameters"]
        actions = {
            branch["properties"]["action"]["const"]
            for branch in schema["oneOf"]
        }
        self.assertEqual(actions, {"create_plan"})

    def test_schema_after_plan_exposes_only_state_valid_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent)
            self.create_plan(agent, turn_id, [
                {"task": "write"},
                {"task": "verify", "completion_policy": "evidence_required"},
            ])

            schema = agent.plan_tool.schema(agent.runtime_state)["function"]["parameters"]
            actions = [
                branch["properties"]["action"]["const"]
                for branch in schema["oneOf"]
            ]

            self.assertNotIn("create_plan", actions)
            self.assertEqual(actions.count("complete_step"), 2)
            self.assertEqual(actions.count("fail_step"), 2)
            self.assertIn("revise_plan", actions)


class CallerOverrideTests(PlanTestCase):
    def test_caller_skip_required_requires_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            turn_id = self.start_turn(agent, mode="required")
            plan_id = "p"
            agent._event(
                "PlanCreated",
                "plan",
                plan_id,
                {
                    "mode": "required",
                    "actor": "model",
                    "reason": "created",
                    "steps": [{"step_id": "step_1", "task": "work", "required": True}],
                },
                turn_id=turn_id,
            )
            with self.assertRaisesRegex(ValueError, "reason"):
                agent.skip_plan_step("step_1", "")
            agent.skip_plan_step("step_1", "caller cancelled work")
            self.assertEqual(
                agent.runtime_state.plans[plan_id].revisions[-1].steps[0].status,
                PlanStepStatus.SKIPPED,
            )


if __name__ == "__main__":
    unittest.main()
