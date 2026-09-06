"""Durable orchestration around existing tool implementations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from memory.event_store import EventStore
from runtime.models import ApprovalDecision, ApprovalRequest, PolicyDecision, RecoveryDecision, ReplayPolicy, ToolExecutionStatus
from runtime.reducer import replay
from tools.base import BaseTool, ToolResult


def _error(message: str) -> ToolResult:
    return ToolResult(raw=message, compact=f"Error: {message}", success=False)


class ToolExecutor:
    def __init__(
        self,
        store: EventStore,
        *,
        get_tool: Callable[[str], BaseTool | None],
        policy: Callable | None = None,
        approval_handler: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
    ):
        self.store = store
        self.get_tool = get_tool
        self.policy = policy or (lambda execution: PolicyDecision.ALLOW)
        self.approval_handler = approval_handler

    def request_batch(self, tool_calls: list, turn_id: str) -> list[str]:
        execution_ids = []
        for tool_call in tool_calls:
            execution_id = str(uuid4())
            execution_ids.append(execution_id)
            tool = self.get_tool(tool_call.function.name)
            replay_policy = tool.replay_policy if tool else ReplayPolicy.MANUAL
            try:
                arguments = json.loads(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            self.store.append_event(
                "ToolRequested",
                "tool_execution",
                execution_id,
                {
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.function.name,
                    "arguments": arguments,
                    "raw_arguments": tool_call.function.arguments,
                    "replay_policy": replay_policy.value,
                    "attempt": 0,
                },
                turn_id=turn_id,
                causation_id=tool_call.id,
                correlation_id=turn_id,
            )
        return execution_ids

    def execute(self, execution_id: str) -> ToolResult:
        state = replay(self.store.read_all(), current_runtime_instance_id=self.store.runtime_instance_id)
        execution = state.executions[execution_id]
        if execution.status != ToolExecutionStatus.PENDING:
            return _error(f"execution {execution_id} is already {execution.status.value}")
        tool = self.get_tool(execution.tool_name)
        raw_arguments = next(
            event["payload"].get("raw_arguments", "{}")
            for event in self.store.read_all()
            if event.get("aggregate_id") == execution_id and event.get("event_type") == "ToolRequested"
        )
        if tool is None:
            return self._fail(execution, f"Unknown tool: {execution.tool_name}", "unknown_tool")
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            return self._fail(execution, "tool call arguments are not valid JSON", "invalid_json")
        validation_error = tool.validate(arguments)
        if validation_error:
            return self._fail(execution, validation_error, "validation")
        self.store.append_event(
            "ToolValidated", "tool_execution", execution_id, {"arguments": arguments},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        execution = replay(self.store.read_all(), current_runtime_instance_id=self.store.runtime_instance_id).executions[execution_id]
        decision = PolicyDecision(self.policy(execution))
        if decision == PolicyDecision.DENY:
            self.store.append_event(
                "ToolCancelled", "tool_execution", execution_id, {"reason": "policy denied"},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            return _error("tool execution denied by policy")
        if decision == PolicyDecision.ASK:
            self.store.append_event(
                "ToolApprovalRequested", "tool_execution", execution_id, {"reason": "policy requires approval"},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            if self.approval_handler is None:
                return _error("tool execution is waiting for approval")
            approval = self.approval_handler(ApprovalRequest(execution_id, execution.tool_name, arguments, "policy requires approval"))
            event_type = "ToolApproved" if approval.approved else "ToolRejected"
            self.store.append_event(
                event_type, "tool_execution", execution_id,
                {"approved_by": approval.approved_by, "note": approval.note},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            if not approval.approved:
                return _error("tool execution rejected by user")
        try:
            recovery_metadata = tool.recovery_metadata(**arguments)
        except Exception as error:
            return self._fail(execution, str(error), "recovery_metadata", type(error).__name__)
        self.store.append_event(
            "ToolStarted", "tool_execution", execution_id,
            {"recovery_metadata": recovery_metadata},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        try:
            if hasattr(tool, "execute_runtime"):
                result = tool.execute_runtime(execution_id, execution.turn_id, **arguments)
            else:
                result = tool.run(**arguments)
        except Exception as error:
            return self._fail(execution, str(error), "exception", type(error).__name__, require_running=True)
        event_type = "ToolCompleted" if result.success else "ToolFailed"
        payload = {"result": {"raw": result.raw, "compact": result.compact, "success": result.success, "exit_code": None}}
        if not result.success:
            payload["error"] = {"category": "tool_result", "message": result.compact}
        self.store.append_event(
            event_type, "tool_execution", execution_id, payload,
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        return result

    def resolve_recovery(
        self,
        execution_id: str,
        decision: RecoveryDecision,
        note: str,
    ) -> ToolResult:
        if not note.strip():
            raise ValueError("recovery decision requires a note")
        state = replay(self.store.read_all())
        execution = state.executions.get(execution_id)
        if execution is None or execution.status != ToolExecutionStatus.RECOVERY_REQUIRED:
            raise ValueError("execution does not require recovery")
        decision = RecoveryDecision(decision)
        if decision == RecoveryDecision.RETRY:
            if execution.replay_policy == ReplayPolicy.MANUAL:
                raise ValueError("manual recovery policy forbids retry")
            if execution.replay_policy == ReplayPolicy.RECONCILABLE:
                current_hash = self._current_hash(execution.recovery_metadata)
                if current_hash != execution.recovery_metadata.get("before_hash"):
                    raise ValueError("file state does not match the pre-effect hash")
            self.store.append_event(
                "ToolRetryScheduled", "tool_execution", execution_id,
                {"note": note}, turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            return self.execute(execution_id)
        if decision == RecoveryDecision.COMPLETED:
            if execution.replay_policy == ReplayPolicy.RECONCILABLE:
                current_hash = self._current_hash(execution.recovery_metadata)
                if current_hash != execution.recovery_metadata.get("expected_after_hash"):
                    raise ValueError("file state does not match the expected result hash")
            result = ToolResult(
                raw=f"Recovered as completed: {note}",
                compact=f"Recovered as completed: {note}",
                success=True,
            )
            self.store.append_event(
                "ToolRecoveredAsCompleted", "tool_execution", execution_id,
                {"note": note, "result": {"raw": result.raw, "compact": result.compact, "success": True, "exit_code": None}},
                turn_id=execution.turn_id, correlation_id=execution.turn_id,
            )
            return result
        result = _error(f"Recovered as failed: {note}")
        self.store.append_event(
            "ToolRecoveredAsFailed", "tool_execution", execution_id,
            {"note": note, "error": {"category": "recovery", "message": note}},
            turn_id=execution.turn_id, correlation_id=execution.turn_id,
        )
        return result

    @staticmethod
    def _current_hash(metadata: dict) -> str:
        path = Path(metadata.get("path", ""))
        if not path.exists():
            return "__missing__"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _fail(
        self,
        execution,
        message: str,
        category: str,
        exception_type: str | None = None,
        *,
        require_running: bool = False,
    ) -> ToolResult:
        result = _error(message)
        self.store.append_event(
            "ToolFailed",
            "tool_execution",
            execution.execution_id,
            {
                "error": {
                    "category": category,
                    "message": message,
                    "exception_type": exception_type,
                    "retryable": False,
                    "details": {},
                },
                "result": {"raw": result.raw, "compact": result.compact, "success": False, "exit_code": None},
            },
            turn_id=execution.turn_id,
            correlation_id=execution.turn_id,
        )
        return result
