"""Agent-local structured control tool for durable plan mutations."""

from __future__ import annotations

from typing import Callable

from runtime.models import CompletionPolicy, PlanStepStatus, ReplayPolicy, RuntimeState, ToolExecutionStatus
from tools.base import BaseTool, ToolResult


class UpdatePlanTool(BaseTool):
    replay_policy = ReplayPolicy.REPLAY_SAFE

    def __init__(self, apply_action: Callable[..., ToolResult]):
        self.apply_action = apply_action
        self.name = "update_plan"
        self.description = "Apply an intent to the durable runtime plan using only actions allowed by the current state frame."
        self.parameters = {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        }

    @staticmethod
    def _create_branch() -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"const": "create_plan"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "minLength": 1},
                            "required": {"type": "boolean"},
                            "completion_policy": {
                                "type": "string",
                                "enum": ["self_attested", "evidence_required"],
                            },
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
                "reason": {"type": "string"},
            },
            "required": ["action", "steps"],
            "additionalProperties": False,
        }

    def schema(self, state: RuntimeState | None = None) -> dict:
        branches = []
        turn = state.turns.get(state.active_turn_id) if state and state.active_turn_id else None
        plan = state.plans.get(turn.plan_id) if turn and turn.plan_id else None
        if plan is None:
            branches.append(self._create_branch())
        else:
            successful_aliases = [
                f"exec_{index}" for index, execution in enumerate(state.executions.values(), 1)
                if execution.status == ToolExecutionStatus.COMPLETED
                and execution.tool_name != "update_plan"
                and execution.result is not None and execution.result.success
            ]
            revision = plan.revisions[-1]
            for step in revision.steps:
                if step.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
                    continue
                properties = {
                    "action": {"const": "complete_step"},
                    "step_id": {"const": step.step_id},
                    "note": {"type": "string", "minLength": 1},
                    "evidence_execution_ids": {
                        "type": "array",
                        "items": ({"type": "string", "enum": successful_aliases} if successful_aliases else {"type": "string"}),
                        "minItems": 1,
                    },
                }
                required = ["action", "step_id"]
                required.append(
                    "evidence_execution_ids"
                    if step.completion_policy == CompletionPolicy.EVIDENCE_REQUIRED
                    else "note"
                )
                branches.append({
                    "type": "object", "properties": properties, "required": required,
                    "additionalProperties": False,
                })
                branches.append({
                    "type": "object",
                    "properties": {
                        "action": {"const": "fail_step"},
                        "step_id": {"const": step.step_id},
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "required": ["action", "step_id", "reason"],
                    "additionalProperties": False,
                })
            step_ids = [step.step_id for step in revision.steps]
            branches.append({
                "type": "object",
                "properties": {
                    "action": {"const": "revise_plan"},
                    "reason": {"type": "string", "minLength": 1},
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_id": {"type": "string", "enum": step_ids},
                                "task": {"type": "string", "minLength": 1},
                                "required": {"type": "boolean"},
                                "completion_policy": {
                                    "type": "string",
                                    "enum": ["self_attested", "evidence_required"],
                                },
                            },
                            "required": ["step_id"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                    },
                },
                "required": ["action", "reason", "patches"],
                "additionalProperties": False,
            })
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "required": ["action"], "oneOf": branches},
            },
        }

    def execute_runtime(self, execution_id: str, turn_id: str, **kwargs) -> ToolResult:
        return self.apply_action(execution_id=execution_id, turn_id=turn_id, **kwargs)

    def validate(self, kwargs):
        # The executor validates the state-dependent schema before this runtime call.
        return None if isinstance(kwargs, dict) and isinstance(kwargs.get('action'), str) else 'invalid plan action'

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult("Runtime context is required", "Error: runtime context is required", False)
