import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.models import ApprovalDecision, PolicyDecision, ToolExecutionStatus
from runtime.reducer import replay
from tools.base import ToolResult
from tests.fakes import FakeTool


def call(call_id="c1", name="fake", arguments="{}"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class ToolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "events.jsonl", session_id="s")
        self.addCleanup(self.store.close)

    def executor(self, tool=None, policy=None, approval=None):
        tool = tool or FakeTool()
        return ToolExecutor(
            self.store,
            get_tool=lambda name: tool if name == tool.name else None,
            policy=policy,
            approval_handler=approval,
        )

    def types(self):
        return [e["event_type"] for e in self.store.read_all()]

    def test_success_emits_requested_validated_started_completed(self):
        executor = self.executor()
        execution_id = executor.request_batch([call()], "t1")[0]
        result = executor.execute(execution_id)
        self.assertTrue(result.success)
        self.assertEqual(self.types(), ["ToolRequested", "ToolValidated", "ToolStarted", "ToolCompleted"])
        execution = replay(self.store.read_all()).executions[execution_id]
        self.assertEqual(execution.status, ToolExecutionStatus.COMPLETED)
        self.assertEqual(execution.result.raw, "raw-ok")
        self.assertEqual(execution.result.compact, "compact-ok")

    def test_all_batch_requests_exist_before_first_effect(self):
        observed = []
        tool = FakeTool()
        original = tool.execute
        def execute(**kwargs):
            observed.extend(self.types())
            return original(**kwargs)
        tool.execute = execute
        executor = self.executor(tool)
        ids = executor.request_batch([call("c1"), call("c2")], "t1")
        executor.execute(ids[0])
        self.assertEqual(observed[:2], ["ToolRequested", "ToolRequested"])

    def test_unknown_tool_invalid_json_and_missing_args_never_start(self):
        required = FakeTool()
        required.parameters = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        executor = self.executor(required)
        calls = [call("a", "missing"), call("b", "fake", "{"), call("c", "fake", "{}")]
        for execution_id in executor.request_batch(calls, "t1"):
            self.assertFalse(executor.execute(execution_id).success)
        self.assertNotIn("ToolStarted", self.types())
        self.assertEqual(required.calls, 0)

    def test_unsuccessful_result_and_exception_become_tool_failed(self):
        for tool in [FakeTool(ToolResult("raw-bad", "compact-bad", False)), FakeTool(error=RuntimeError("boom"))]:
            with self.subTest(tool=tool.error):
                executor = self.executor(tool)
                execution_id = executor.request_batch([call()], "t1")[0]
                result = executor.execute(execution_id)
                self.assertFalse(result.success)
                self.assertEqual(replay(self.store.read_all()).executions[execution_id].status, ToolExecutionStatus.FAILED)

    def test_system_exit_leaves_started_execution_for_recovery(self):
        executor = self.executor(FakeTool(error=SystemExit(2)))
        execution_id = executor.request_batch([call()], "t1")[0]
        with self.assertRaises(SystemExit):
            executor.execute(execution_id)
        self.assertEqual(replay(self.store.read_all()).executions[execution_id].status, ToolExecutionStatus.RECOVERY_REQUIRED)

    def test_policy_deny_and_approval_reject_never_call_tool(self):
        denied = FakeTool()
        executor = self.executor(denied, policy=lambda execution: PolicyDecision.DENY)
        result = executor.execute(executor.request_batch([call()], "t1")[0])
        self.assertFalse(result.success)
        self.assertEqual(denied.calls, 0)
        asked = FakeTool()
        executor = self.executor(
            asked,
            policy=lambda execution: PolicyDecision.ASK,
            approval=lambda request: ApprovalDecision(False, note="no"),
        )
        result = executor.execute(executor.request_batch([call("c2")], "t1")[0])
        self.assertFalse(result.success)
        self.assertEqual(asked.calls, 0)
        self.assertIn("ToolApprovalRequested", self.types())
        self.assertIn("ToolRejected", self.types())

    def test_terminal_execution_cannot_start_twice(self):
        tool = FakeTool()
        executor = self.executor(tool)
        execution_id = executor.request_batch([call()], "t1")[0]
        executor.execute(execution_id)
        duplicate = executor.execute(execution_id)
        self.assertFalse(duplicate.success)
        self.assertEqual(tool.calls, 1)


if __name__ == "__main__":
    unittest.main()

class PersistFailureTests(unittest.TestCase):
    def test_persist_failure_before_started_prevents_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.jsonl")
            self.addCleanup(store.close)
            tool = FakeTool()
            executor = ToolExecutor(store, get_tool=lambda name: tool)
            execution_id = executor.request_batch([call()], "t1")[0]
            original = store.append_event
            def fail_started(event_type, *args, **kwargs):
                if event_type == "ToolStarted":
                    raise OSError("disk full")
                return original(event_type, *args, **kwargs)
            with patch.object(store, "append_event", side_effect=fail_started), self.assertRaisesRegex(OSError, "disk full"):
                executor.execute(execution_id)
            self.assertEqual(tool.calls, 0)

class SecretArgumentTests(unittest.TestCase):
    def test_secret_argument_executes_but_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = EventStore(path)
            self.addCleanup(store.close)
            tool = FakeTool()
            tool.parameters = {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}
            executor = ToolExecutor(store, get_tool=lambda name: tool)
            execution_id = executor.request_batch([call(arguments='{"token":"secret-value"}')], "t1")[0]
            self.assertTrue(executor.execute(execution_id).success)
            self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))
