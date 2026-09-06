"""Pure projection of validated runtime events into deterministic state."""

from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from memory.event_store import JournalCorruptionError
from runtime.models import (
    ApprovalMetadata,
    CompletionBlocker,
    CompletionPolicy,
    ErrorRecord,
    PendingRuntimeAction,
    Plan,
    PlanMode,
    PlanRevision,
    PlanStep,
    PlanStepStatus,
    ReplayPolicy,
    RuntimeState,
    ToolExecution,
    ToolExecutionStatus,
    ToolResultData,
    TurnState,
    TurnStatus,
)

_TERMINAL_TURNS = {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED, TurnStatus.FAILED}
_TERMINAL_TOOLS = {ToolExecutionStatus.COMPLETED, ToolExecutionStatus.FAILED, ToolExecutionStatus.CANCELLED}


def _error(payload: dict) -> ErrorRecord:
    data = payload.get("error", payload)
    return ErrorRecord(
        category=data.get("category", "runtime"),
        message=data.get("message", "unknown runtime error"),
        exception_type=data.get("exception_type"),
        retryable=bool(data.get("retryable", False)),
        details=dict(data.get("details", {})),
    )


def _steps(payload: list[dict]) -> tuple[PlanStep, ...]:
    return tuple(
        PlanStep(
            step_id=item["step_id"],
            task=item["task"],
            status=PlanStepStatus(item.get("status", "pending")),
            required=bool(item.get("required", True)),
            completion_policy=CompletionPolicy(item.get("completion_policy", "self_attested")),
            note=item.get("note"),
            evidence_execution_ids=list(item.get("evidence_execution_ids", [])),
        )
        for item in payload
    )


def reduce_event(state: RuntimeState, event: dict) -> RuntimeState:
    if "event_type" not in event:
        return state
    kind = event["event_type"]
    aggregate_id = event["aggregate_id"]
    payload = event["payload"]
    ts = event["ts"]

    if kind == "TurnStarted":
        if state.active_turn_id is not None or aggregate_id in state.turns:
            raise ValueError("cannot start a duplicate or concurrent turn")
        turn = TurnState(
            turn_id=aggregate_id,
            session_id=event["session_id"],
            goal=payload["goal"],
            status=TurnStatus.RUNNING,
            started_at=ts,
            plan_mode=PlanMode(payload.get("plan_mode", "optional")),
            resumes_turn_id=payload.get("resumes_turn_id"),
        )
        state.turns[aggregate_id] = turn
        state.active_turn_id = aggregate_id
    elif kind.startswith("Turn") or kind in {"CompletionRequested", "CompletionBlocked"}:
        turn = state.turns.get(aggregate_id)
        if turn is None:
            raise ValueError("turn event references missing turn")
        if turn.status in _TERMINAL_TURNS:
            raise ValueError("terminal turn cannot transition")
        if kind == "TurnIterationAdvanced":
            iteration = int(payload["iteration"])
            if iteration != turn.iteration + 1:
                raise ValueError("turn iteration is not consecutive")
            turn.iteration = iteration
        elif kind == "CompletionBlocked":
            turn.completion_block_count += 1
        elif kind == "TurnCompleted":
            turn.status = TurnStatus.COMPLETED
            turn.ended_at = ts
            turn.final_text = payload.get("final_text")
            state.active_turn_id = None
        elif kind == "TurnInterrupted":
            turn.status = TurnStatus.INTERRUPTED
            turn.ended_at = ts
            state.active_turn_id = None
        elif kind == "TurnFailed":
            turn.status = TurnStatus.FAILED
            turn.ended_at = ts
            turn.error = _error(payload)
            state.active_turn_id = None

    elif kind == "PlanCreated":
        if aggregate_id in state.plans:
            raise ValueError("duplicate plan")
        plan = Plan(
            plan_id=aggregate_id,
            session_id=event["session_id"],
            mode=PlanMode(payload.get("mode", "optional")),
            active_revision=1,
            revisions=[PlanRevision(1, ts, payload.get("actor", "model"), payload.get("reason", "created"), _steps(payload["steps"]))],
        )
        state.plans[aggregate_id] = plan
        state.active_plan_id = aggregate_id
        turn = state.turns.get(event.get("turn_id"))
        if turn:
            turn.plan_id = aggregate_id
            turn.completion_block_count = 0
    elif kind.startswith("Plan"):
        plan = state.plans.get(aggregate_id)
        if plan is None:
            raise ValueError("plan event references missing plan")
        current = plan.revisions[-1]
        if kind == "PlanRevised":
            revision = int(payload["to_revision"])
            if revision != plan.active_revision + 1:
                raise ValueError("plan revision is not consecutive")
            plan.revisions.append(PlanRevision(revision, ts, payload["actor"], payload["reason"], _steps(payload["steps"])))
            plan.active_revision = revision
        else:
            step_id = payload["step_id"]
            step = next((item for item in current.steps if item.step_id == step_id), None)
            if step is None:
                raise ValueError("plan step does not exist")
            transitions = {
                "PlanStepStarted": ({PlanStepStatus.PENDING, PlanStepStatus.FAILED}, PlanStepStatus.IN_PROGRESS),
                "PlanStepCompleted": ({PlanStepStatus.IN_PROGRESS}, PlanStepStatus.COMPLETED),
                "PlanStepFailed": ({PlanStepStatus.IN_PROGRESS}, PlanStepStatus.FAILED),
                "PlanStepSkipped": ({PlanStepStatus.PENDING, PlanStepStatus.FAILED}, PlanStepStatus.SKIPPED),
            }
            allowed, target = transitions[kind]
            if step.status not in allowed:
                raise ValueError("invalid plan step transition")
            step.status = target
            step.note = payload.get("note")
            step.evidence_execution_ids = list(payload.get("evidence_execution_ids", []))
        turn = state.turns.get(event.get("turn_id"))
        if turn:
            turn.completion_block_count = 0

    elif kind == "ToolRequested":
        if aggregate_id in state.executions:
            raise ValueError("duplicate tool execution")
        state.executions[aggregate_id] = ToolExecution(
            execution_id=aggregate_id,
            tool_call_id=payload["tool_call_id"],
            session_id=event["session_id"],
            turn_id=event["turn_id"],
            tool_name=payload["tool_name"],
            arguments=dict(payload.get("arguments", {})),
            status=ToolExecutionStatus.PENDING,
            requested_at=ts,
            replay_policy=ReplayPolicy(payload.get("replay_policy", "manual")),
            attempt=int(payload.get("attempt", 0)),
            recovery_metadata=dict(payload.get("recovery_metadata", {})),
        )
    elif kind in {
        "ToolValidated", "ToolApprovalRequested", "ToolApproved", "ToolRejected",
        "ToolStarted", "ToolCompleted", "ToolFailed", "ToolCancelled",
        "ToolRecoveryRequired", "ToolRecoveredAsCompleted", "ToolRecoveredAsFailed",
        "ToolRetryScheduled",
    }:
        execution = state.executions.get(aggregate_id)
        if execution is None:
            raise ValueError("tool event references missing execution")
        if execution.status in _TERMINAL_TOOLS:
            raise ValueError("terminal tool execution cannot transition")
        if kind == "ToolValidated":
            if execution.status != ToolExecutionStatus.PENDING:
                raise ValueError("only pending tool can validate")
            execution.arguments = dict(payload.get("arguments", execution.arguments))
        elif kind == "ToolApprovalRequested":
            if execution.status != ToolExecutionStatus.PENDING:
                raise ValueError("only pending tool can request approval")
            execution.status = ToolExecutionStatus.WAITING_APPROVAL
        elif kind == "ToolApproved":
            if execution.status != ToolExecutionStatus.WAITING_APPROVAL:
                raise ValueError("only waiting tool can be approved")
            execution.status = ToolExecutionStatus.PENDING
            execution.approval = ApprovalMetadata(True, payload.get("approved_by", "user"), ts, payload.get("note"))
        elif kind == "ToolRejected":
            if execution.status != ToolExecutionStatus.WAITING_APPROVAL:
                raise ValueError("only waiting tool can be rejected")
            execution.status = ToolExecutionStatus.CANCELLED
            execution.finished_at = ts
            execution.approval = ApprovalMetadata(False, payload.get("approved_by", "user"), ts, payload.get("note"))
        elif kind == "ToolStarted":
            if execution.status != ToolExecutionStatus.PENDING:
                raise ValueError("only pending tool can start")
            execution.status = ToolExecutionStatus.RUNNING
            execution.started_at = ts
            execution.recovery_metadata.update(payload.get("recovery_metadata", {}))
            execution.recovery_metadata["_started_runtime_instance_id"] = event["runtime_instance_id"]
        elif kind == "ToolCompleted":
            if execution.status not in {ToolExecutionStatus.RUNNING, ToolExecutionStatus.RECOVERY_REQUIRED}:
                raise ValueError("tool cannot complete from current state")
            execution.status = ToolExecutionStatus.COMPLETED
            execution.finished_at = ts
            execution.result = ToolResultData(**payload["result"])
        elif kind == "ToolFailed":
            if execution.status not in {ToolExecutionStatus.PENDING, ToolExecutionStatus.RUNNING, ToolExecutionStatus.RECOVERY_REQUIRED}:
                raise ValueError("tool cannot fail from current state")
            execution.status = ToolExecutionStatus.FAILED
            execution.finished_at = ts
            execution.error = _error(payload)
            if "result" in payload:
                execution.result = ToolResultData(**payload["result"])
        elif kind == "ToolCancelled":
            if execution.status not in {ToolExecutionStatus.PENDING, ToolExecutionStatus.WAITING_APPROVAL}:
                raise ValueError("tool cannot cancel from current state")
            execution.status = ToolExecutionStatus.CANCELLED
            execution.finished_at = ts
        elif kind == "ToolRecoveryRequired":
            if execution.status != ToolExecutionStatus.RUNNING:
                raise ValueError("only running tool can require recovery")
            execution.status = ToolExecutionStatus.RECOVERY_REQUIRED
        elif kind == "ToolRecoveredAsCompleted":
            if execution.status != ToolExecutionStatus.RECOVERY_REQUIRED:
                raise ValueError("only recovery-required tool can reconcile")
            execution.status = ToolExecutionStatus.COMPLETED
            execution.finished_at = ts
            if "result" in payload:
                execution.result = ToolResultData(**payload["result"])
        elif kind == "ToolRecoveredAsFailed":
            if execution.status != ToolExecutionStatus.RECOVERY_REQUIRED:
                raise ValueError("only recovery-required tool can reconcile")
            execution.status = ToolExecutionStatus.FAILED
            execution.finished_at = ts
            execution.error = _error(payload)
        elif kind == "ToolRetryScheduled":
            if execution.status != ToolExecutionStatus.RECOVERY_REQUIRED:
                raise ValueError("only recovery-required tool can retry")
            execution.status = ToolExecutionStatus.PENDING
            execution.attempt += 1
        turn = state.turns.get(event.get("turn_id"))
        if turn and kind in {"ToolCompleted", "ToolFailed", "ToolRejected", "ToolCancelled", "ToolRecoveredAsCompleted", "ToolRecoveredAsFailed"}:
            turn.completion_block_count = 0
    return state


def replay(events: Iterable[dict], current_runtime_instance_id: str | None = None) -> RuntimeState:
    materialized = list(events)
    session_id = next((event.get("session_id") for event in materialized if event.get("session_id")), "legacy")
    state = RuntimeState(session_id=session_id)
    for event in materialized:
        try:
            reduce_event(state, event)
        except (KeyError, TypeError, ValueError) as error:
            raise JournalCorruptionError(f"invalid historical transition at seq {event.get('seq')}: {error}") from error
    for execution in state.executions.values():
        started_instance = execution.recovery_metadata.get("_started_runtime_instance_id")
        if execution.status == ToolExecutionStatus.RUNNING and (
            current_runtime_instance_id is None or started_instance != current_runtime_instance_id
        ):
            execution.status = ToolExecutionStatus.RECOVERY_REQUIRED
    return state


def completion_blockers(state: RuntimeState, turn_id: str) -> list[CompletionBlocker]:
    turn = state.turns[turn_id]
    blockers = []
    plan = state.plans.get(turn.plan_id) if turn.plan_id else None
    if turn.plan_mode == PlanMode.REQUIRED and plan is None:
        blockers.append(CompletionBlocker("missing_required_plan", "A required plan has not been created."))
    if plan:
        unresolved = tuple(
            step.step_id for step in plan.revisions[-1].steps
            if step.required and step.status not in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}
        )
        if unresolved:
            blockers.append(CompletionBlocker("unfinished_required_steps", "Required plan steps are unfinished.", unresolved))
    unresolved_tools = tuple(
        execution.execution_id for execution in state.executions.values()
        if execution.turn_id == turn_id and execution.status in {
            ToolExecutionStatus.WAITING_APPROVAL,
            ToolExecutionStatus.RUNNING,
            ToolExecutionStatus.RECOVERY_REQUIRED,
        }
    )
    if unresolved_tools:
        blockers.append(CompletionBlocker("unresolved_tool_executions", "Tool executions require resolution.", unresolved_tools))
    return blockers


def pending_runtime_actions(state: RuntimeState) -> list[PendingRuntimeAction]:
    actions = []
    for execution in state.executions.values():
        if execution.status == ToolExecutionStatus.WAITING_APPROVAL:
            actions.append(PendingRuntimeAction("approval", execution.execution_id, execution.tool_name, "Approval required"))
        elif execution.status == ToolExecutionStatus.RECOVERY_REQUIRED:
            actions.append(PendingRuntimeAction("recovery", execution.execution_id, execution.tool_name, "Outcome is unknown"))
    return actions


def runtime_prompt_projection(state: RuntimeState) -> str:
    if state.active_turn_id is None:
        return "(no active turn)"
    turn = state.turns[state.active_turn_id]
    lines = [f"Turn {turn.turn_id}: {turn.status.value}", f"Goal: {turn.goal}", f"Iteration: {turn.iteration}"]
    for blocker in completion_blockers(state, turn.turn_id):
        lines.append(f"Blocker {blocker.code}: {blocker.message} {' '.join(blocker.ids)}".rstrip())
    return "\n".join(lines)
