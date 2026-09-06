import hashlib
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

    def test_expected_file_hash_recovers_completed_without_write(self):
        store = self.store()
        target = Path(self.directory.name) / "out.txt"
        target.write_text("after", encoding="utf-8")
        expected = hashlib.sha256(b"after").hexdigest()
        store.append_event("ToolRequested", "tool_execution", "x", {
            "tool_call_id": "c", "tool_name": "write", "arguments": {"path": str(target), "content": "after"},
            "replay_policy": "reconcilable", "recovery_metadata": {"before_hash": "__missing__", "expected_after_hash": expected, "path": str(target)}
        }, turn_id="t")
        store.append_event("ToolStarted", "tool_execution", "x", {"recovery_metadata": {"before_hash": "__missing__", "expected_after_hash": expected, "path": str(target)}}, turn_id="t")
        executor = ToolExecutor(store, get_tool=lambda name: WriteTool())
        with patch.object(WriteTool, "execute", side_effect=AssertionError("must not write")):
            result = executor.resolve_recovery("x", RecoveryDecision.COMPLETED, "hash matched")
        self.assertTrue(result.success)

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


if __name__ == "__main__":
    unittest.main()
