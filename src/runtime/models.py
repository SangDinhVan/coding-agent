from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TurnStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class PlanMode(str, Enum):
    OPTIONAL = "optional"
    REQUIRED = "required"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CompletionPolicy(str, Enum):
    SELF_ATTESTED = "self_attested"
    EVIDENCE_REQUIRED = "evidence_required"


class ToolExecutionStatus(str, Enum):
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplayPolicy(str, Enum):
    REPLAY_SAFE = "replay_safe"
    RECONCILABLE = "reconcilable"
    MANUAL = "manual"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RecoveryDecision(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"


class TurnTransition(str, Enum):
    START = "start"
    COMPLETE = "complete"
    INTERRUPT = "interrupt"
    FAIL = "fail"


class PlanStepTransition(str, Enum):
    START = "start"
    COMPLETE = "complete"
    FAIL = "fail"
    SKIP = "skip"
    RESET = "reset"


class ToolTransition(str, Enum):
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    REJECT = "reject"
    START = "start"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"
    REQUIRE_RECOVERY = "require_recovery"
    RECOVER_COMPLETE = "recover_complete"
    RECOVER_FAIL = "recover_fail"
    RETRY = "retry"


@dataclass(frozen=True)
class RuntimeEvent:
    schema_version: int
    event_id: str
    seq: int
    ts: str
    session_id: str
    runtime_instance_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    turn_id: str | None
    causation_id: str | None
    correlation_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class ErrorRecord:
    category: str
    message: str
    exception_type: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalMetadata:
    approved: bool
    approved_by: str
    timestamp: str
    note: str | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    execution_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    approved_by: str = "user"
    note: str | None = None


@dataclass(frozen=True)
class CompletionBlocker:
    code: str
    message: str
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingRuntimeAction:
    kind: str
    execution_id: str
    tool_name: str
    message: str


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    goal: str
    status: TurnStatus
    started_at: str
    ended_at: str | None = None
    iteration: int = 0
    plan_id: str | None = None
    plan_mode: PlanMode = PlanMode.OPTIONAL
    completion_block_count: int = 0
    resumes_turn_id: str | None = None
    final_text: str | None = None
    error: ErrorRecord | None = None


@dataclass
class PlanStep:
    step_id: str
    task: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    required: bool = True
    completion_policy: CompletionPolicy = CompletionPolicy.SELF_ATTESTED
    note: str | None = None
    evidence_execution_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanRevision:
    revision: int
    created_at: str
    created_by: str
    reason: str
    steps: tuple[PlanStep, ...]


@dataclass
class Plan:
    plan_id: str
    session_id: str
    mode: PlanMode
    active_revision: int
    revisions: list[PlanRevision]


@dataclass(frozen=True)
class ToolResultData:
    raw: str
    compact: str
    success: bool
    exit_code: int | None = None


@dataclass
class ToolExecution:
    execution_id: str
    tool_call_id: str
    session_id: str
    turn_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: ToolExecutionStatus
    requested_at: str
    started_at: str | None = None
    finished_at: str | None = None
    attempt: int = 0
    replay_policy: ReplayPolicy = ReplayPolicy.MANUAL
    approval: ApprovalMetadata | None = None
    result: ToolResultData | None = None
    error: ErrorRecord | None = None
    retry_of_execution_id: str | None = None
    recovery_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeState:
    session_id: str
    turns: dict[str, TurnState] = field(default_factory=dict)
    plans: dict[str, Plan] = field(default_factory=dict)
    executions: dict[str, ToolExecution] = field(default_factory=dict)
    active_turn_id: str | None = None
    active_plan_id: str | None = None
