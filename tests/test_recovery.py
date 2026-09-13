import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.loop import Agent
from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.models import RecoveryDecision, ReplayPolicy, ToolExecutionStatus
from runtime.reducer import replay
from tools.base import BaseTool, ToolResult
from tools.filesystem import WriteTool
from tools.terminal import BashTool
from tests.fakes import FakeTool, stream_text


class FakeRegistry:
    def __init__(self, tool):
        self.tool = tool

    def schemas(self):
        return []

    def get(self, name):
        return self.tool if name == self.tool.name else None


def call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.jsonl"

    def store(self, runtime="r1"):
        store = EventStore(self.path, session_id="s", runtime_instance_id=runtime)
        self.addCleanup(store.close)
        return store

    def test_current_instance_running_is_not_marked_stale(self):
        store = self.store()
        store.append_event("TurnStarted", "turn", "t1", {"goal": "g"}, turn_id="t1")
        store.append_event("ToolRequested", "tool_execution", "x1", {
            "tool_call_id": "c", "tool_name": "fake", "arguments": {}, "replay_policy": "manual"
        }, turn_id="t1")
        store.append_event("ToolStarted", "tool_execution", "x1", {}, turn_id="t1")
        self.assertEqual(replay(store.read_all(), current_runtime_instance_id="r1").executions["x1"].status, ToolExecutionStatus.RUNNING)
        self.assertEqual(replay(store.read_all(), current_runtime_instance_id="r2").executions["x1"].status, ToolExecutionStatus.RECOVERY_REQUIRED)

    def test_crash_after_started_requires_recovery_and_completed_never_reexecutes(self):
        store = self.store()
        tool = FakeTool(error=SystemExit(2))
        executor = ToolExecutor(store, get_tool=lambda name: tool)
        execution_id = executor.request_batch([call("c", "fake", "{}")], "t1")[0]
        with self.assertRaises(SystemExit):
            executor.execute(execution_id)
        store.close()
        reopened = EventStore(self.path, runtime_instance_id="r2")
        self.addCleanup(reopened.close)
        state = replay(reopened.read_all(), current_runtime_instance_id="r2")
        self.assertEqual(state.executions[execution_id].status, ToolExecutionStatus.RECOVERY_REQUIRED)
        retry_executor = ToolExecutor(reopened, get_tool=lambda name: tool)
        result = retry_executor.resolve_recovery(execution_id, RecoveryDecision.COMPLETED, "verified externally")
        self.assertTrue(result.success)
        self.assertEqual(tool.calls, 1)

    def test_bash_unknown_outcome_rejects_retry(self):
        store = self.store()
        store.append_event("ToolRequested", "tool_execution", "x", {
            "tool_call_id": "c", "tool_name": "bash", "arguments": {"command": "echo x"}, "replay_policy": "manual"
        }, turn_id="t")
        store.append_event("ToolStarted", "tool_execution", "x", {}, turn_id="t")
        executor = ToolExecutor(store, get_tool=lambda name: BashTool())
        with self.assertRaisesRegex(ValueError, "manual"):
            executor.resolve_recovery("x", RecoveryDecision.RETRY, "try again")

    def test_expected_file_hash_recovers_completed_through_tool_boundary(self):
        class ReconcilableTool(FakeTool):
            replay_policy = ReplayPolicy.RECONCILABLE
            def __init__(self):
                super().__init__()
                self.fingerprints = []
            def recovery_fingerprint(self, metadata):
                self.fingerprints.append(metadata)
                return "expected"

        store = self.store()
        metadata = {"before_hash": "before", "expected_after_hash": "expected", "path": "out.txt"}
        store.append_event("ToolRequested", "tool_execution", "x", {
            "tool_call_id": "c", "tool_name": "fake", "arguments": {},
            "replay_policy": "reconcilable", "recovery_metadata": metadata,
        }, turn_id="t")
        store.append_event("ToolStarted", "tool_execution", "x", {"recovery_metadata": metadata}, turn_id="t")
        tool = ReconcilableTool()
        executor = ToolExecutor(store, get_tool=lambda name: tool)
        with patch.object(tool, "execute", side_effect=AssertionError("must not execute")):
            result = executor.resolve_recovery("x", RecoveryDecision.COMPLETED, "hash matched")
        self.assertTrue(result.success)
        self.assertEqual(tool.fingerprints, [metadata | {"_started_runtime_instance_id": "r1"}])

    def test_resume_active_turn_does_not_duplicate_user_message(self):
        first = Agent(str(self.path), workdir=self.directory.name)
        first.event_store.append("user", "goal", turn_id="t1")
        first._event("TurnStarted", "turn", "t1", {"goal": "goal", "plan_mode": "optional"}, turn_id="t1")
        first.close()
        resumed = Agent(str(self.path), workdir=self.directory.name)
        self.addCleanup(resumed.close)
        with patch.object(Agent, "_update_memory_bg", return_value=None), patch("agent.loop.llm.complete", return_value=stream_text("done")):
            self.assertEqual(resumed.resume_active_turn(), "done")
        users = [m for m in resumed.event_store.to_messages() if m["role"] == "user"]
        self.assertEqual(len(users), 1)

    def test_legacy_tool_result_without_metadata_still_replays(self):
        store = self.store()
        store.append_event("ToolRequested", "tool_execution", "x", {
            "tool_call_id": "c", "tool_name": "fake", "arguments": {}, "replay_policy": "manual",
        }, turn_id="t")
        store.append_event("ToolStarted", "tool_execution", "x", {}, turn_id="t")
        store.append_event("ToolCompleted", "tool_execution", "x", {
            "result": {"raw": "ok", "compact": "ok", "success": True, "exit_code": 0},
        }, turn_id="t")
        self.assertEqual(replay(store.read_all()).executions["x"].result.metadata, {})


if __name__ == "__main__":
    unittest.main()

class ApprovalProgressTests(RecoveryTests):
    def test_waiting_approval_blocks_model_progress(self):
        fake = FakeTool()
        agent = Agent(
            str(self.path),
            workdir=self.directory.name,
            policy=lambda execution: "ask",
            approval_handler=None,
            tool_registry=FakeRegistry(fake),
        )
        self.addCleanup(agent.close)
        responses = [iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=0, id="c", type="function", function=SimpleNamespace(name="fake", arguments="{}"))]))])]), stream_text("must not happen")]
        with patch("agent.loop.llm.complete", side_effect=responses) as complete:
            self.assertEqual(agent.run_turn("g"), "")
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(fake.calls, 0)

class BatchResumeTests(RecoveryTests):
    def test_resume_drains_pending_batch_before_model_progress(self):
        fake = FakeTool()
        agent = Agent(
            str(self.path), workdir=self.directory.name,
            policy=lambda execution: "ask" if execution.tool_call_id == "c1" else "allow",
            approval_handler=None,
            tool_registry=FakeRegistry(fake),
        )
        self.addCleanup(agent.close)
        first_response = iter([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[
                SimpleNamespace(index=0, id="c1", type="function", function=SimpleNamespace(name="fake", arguments="{}")),
                SimpleNamespace(index=1, id="c2", type="function", function=SimpleNamespace(name="fake", arguments="{}")),
            ]))])
        ])
        with patch("agent.loop.llm.complete", return_value=first_response):
            agent.run_turn("g")
        approval = agent.pending_runtime_actions()[0]
        agent.resolve_approval(approval.execution_id, True, "approved")
        with (
            patch.object(Agent, "_update_memory_bg", return_value=None),
            patch("agent.loop.llm.complete", return_value=stream_text("done")) as complete,
        ):
            agent.resume_active_turn()
        self.assertEqual(fake.calls, 2)
        self.assertEqual(complete.call_count, 1)
        tool_messages = [m for m in agent.event_store.to_messages() if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["c1", "c2"])

    def test_persisted_final_completes_without_model_call(self):
        first = Agent(str(self.path), workdir=self.directory.name)
        first.event_store.append("user", "goal", turn_id="t1")
        first._event("TurnStarted", "turn", "t1", {"goal": "goal", "plan_mode": "optional"}, turn_id="t1")
        first.event_store.append("assistant", "already final", turn_id="t1", final=True)
        first.close()
        resumed = Agent(str(self.path), workdir=self.directory.name)
        self.addCleanup(resumed.close)
        with patch("agent.loop.llm.complete") as complete:
            self.assertEqual(resumed.resume_active_turn(), "already final")
        complete.assert_not_called()
