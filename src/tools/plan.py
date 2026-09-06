"""Agent-local structured control tool for durable plan mutations."""

from __future__ import annotations

from typing import Callable

from runtime.models import ReplayPolicy
from tools.base import BaseTool, ToolResult


class UpdatePlanTool(BaseTool):
    replay_policy = ReplayPolicy.REPLAY_SAFE

    def __init__(self, apply_action: Callable[..., ToolResult]):
        self.apply_action = apply_action
        self.name = "update_plan"
        self.description = "Create, revise, or update the status of the durable runtime plan."
        self.parameters = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "set_step_status", "revise"]},
                "steps": {"type": "array", "items": {"type": "object"}},
                "step_id": {"type": "string"},
                "status": {"type": "string", "enum": ["in_progress", "completed", "failed", "skipped"]},
                "note": {"type": "string"},
                "reason": {"type": "string"},
                "evidence_execution_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        }

    def execute_runtime(self, execution_id: str, turn_id: str, **kwargs) -> ToolResult:
        return self.apply_action(execution_id=execution_id, turn_id=turn_id, **kwargs)

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult("Runtime context is required", "Error: runtime context is required", False)
