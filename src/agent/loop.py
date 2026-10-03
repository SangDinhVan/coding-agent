"""Model-driven agent loop with durable runtime lifecycle boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

from agent.state import WorkspaceState
from context.checkpoint import CheckpointCorruptionError, CheckpointStore
from context.compactor import Compactor
from core.paths import checkpoint_path, project_memory_path, workspace_identity as compute_workspace_identity
from memory.event_store import EventStore
from memory.manager import MemoryManager
from model import llm
from runtime.executor import ToolExecutor
from runtime.models import CompletionPolicy, PlanMode, PlanStepStatus, RecoveryDecision, ToolExecutionStatus, TurnStatus
from runtime.reducer import completion_blockers, pending_runtime_actions, replay, runtime_prompt_projection, runtime_state_frame
from sandbox.changes import apply_changeset, load_changeset
from sandbox.models import SandboxStatus, WorkspaceMode
from tools.base import ToolResult
from tools.plan import UpdatePlanTool
from tools.registry import ToolRegistry


def encode_image(path: str) -> tuple[str, str]:
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None:
        mime_type = "image/png"
    with open(path, "rb") as file:
        return mime_type, base64.b64encode(file.read()).decode("utf-8")


def build_user_content(text: str, image_paths: list[str] | None = None):
    if not image_paths:
        return text
    content = [{"type": "text", "text": text}]
    for path in image_paths:
        mime_type, encoded = encode_image(path)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}})
    return content


class StreamedToolCall:
    def __init__(self):
        self.id = ""
        self.type = "function"
        self.function = SimpleNamespace(name="", arguments="")

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


SYSTEM_PROMPT_TEMPLATE = """\
Bạn là 1 coding agent cá nhân, có quyền đọc/ghi/sửa file và chạy lệnh shell thông qua các tool được cung cấp.

Tác vụ đơn giản có thể hoàn thành trực tiếp thì không tạo plan.
State frame là nguồn sự thật duy nhất cho plan và allowed_actions. Chỉ gọi intent
đang được cho phép; complete_step/fail_step tự thực thi lifecycle hợp lệ bên trong
runtime. Step evidence_required phải dùng Execution ID dạng exec_1, exec_2 từ kết
quả tool thành công.
Khi kiểm tra, structural chỉ xác nhận cấu trúc/static; behavioral phải thực sự
chạy hành vi, test hoặc trình duyệt. Không gọi structural là behavioral.

--- PROJECT.md (advisory facts về project) ---
{project_md}

Memory is advisory. Current system/user instructions, repository code and
configuration, tests, session journal, and runtime state take precedence over
PROJECT.md whenever they conflict.

--- Trạng thái runtime hiện tại ---
{runtime_state}

--- Workspace ---
{workspace_state}
"""


def tool_message_content(result: ToolResult, execution_id: str) -> str:
    verification_kind = result.metadata.get("verification_kind")
    if verification_kind and verification_kind != "unclassified":
        verification = json.dumps({
            "verification_kind": verification_kind,
            "passed": bool(result.metadata.get("passed")),
            "execution_alias": execution_id,
        }, separators=(",", ":"))
        return f"{result.compact}\nVerification: {verification}"
    if not result.success:
        return result.compact
    return f"{result.compact}\nExecution ID: {execution_id}"


class Agent:
    def __init__(
        self,
        events_path: str,
        model: Optional[str] = None,
        workdir: str = ".",
        policy=None,
        approval_handler=None,
        sandbox=None,
        tool_registry=None,
        state_root: str | Path | None = None,
        workspace_identity: str | None = None,
    ):
        self.model = model
        self.workdir = Path(workdir).resolve()
        self.event_store = EventStore(path=events_path)
        self.state_root = Path(state_root) if state_root is not None else Path(events_path).parent
        self.workspace_identity = workspace_identity or compute_workspace_identity(workdir)
        self.memory_manager = MemoryManager(
            project_memory_path(self.state_root, self.workspace_identity)
        )
        self.checkpoint_store = CheckpointStore(
            checkpoint_path(self.state_root, self.event_store.session_id)
        )
        try:
            self.checkpoint = self.checkpoint_store.load(
                self.event_store.session_id, self.event_store.last_seq
            )
        except CheckpointCorruptionError as error:
            print(f"[context] ignoring invalid checkpoint: {error}", flush=True)
            self.checkpoint = None
        self.compactor = Compactor(model=model)
        self.sandbox = sandbox
        self.tool_registry = tool_registry or ToolRegistry(sandbox)
        self.workspace_state = WorkspaceState(sandbox=sandbox)
        self.workspace_mode = getattr(sandbox, "mode", WorkspaceMode.SHADOW)
        self.last_changes = (
            load_changeset(sandbox.paths)
            if sandbox is not None
            and self.workspace_mode == WorkspaceMode.SHADOW
            and sandbox.status == SandboxStatus.SEALED
            else None
        )
        self.runtime_state = replay(self.event_store.read_all(), current_runtime_instance_id=self.event_store.runtime_instance_id)
        self.turn_state = self._latest_turn()
        self.plan_tool = UpdatePlanTool(self._apply_plan_action)
        self.tool_executor = ToolExecutor(
            self.event_store,
            get_tool=lambda name: self.plan_tool if name == "update_plan" else self.tool_registry.get(name),
            policy=policy,
            approval_handler=approval_handler,
        )

    def close(self) -> None:
        try:
            if self.sandbox is not None:
                status = getattr(getattr(self.sandbox, "status", None), "value", None)
                if status in {"running", "created", "error"}:
                    self.sandbox.stop("agent_close")
        finally:
            llm.finish_trace()
            self.event_store.close()

    def prepare_changes(self):
        if self.sandbox is None:
            raise RuntimeError("sandbox is unavailable")
        if self.workspace_mode == WorkspaceMode.LIVE:
            raise RuntimeError("live workspace changes are already applied")
        self.last_changes = self.sandbox.prepare_changes()
        return self.last_changes

    def apply_changes(self, approved_hash: str, note: str):
        if self.workspace_mode == WorkspaceMode.LIVE:
            raise RuntimeError("live workspace changes are already applied")
        if self.sandbox is None or self.last_changes is None:
            raise RuntimeError("run /changes before /apply")
        result = apply_changeset(
            self.sandbox.paths,
            self.sandbox.source_workspace,
            self.last_changes,
            approved_hash,
            note,
        )
        self.sandbox.destroy("applied")
        return result

    def discard(self) -> None:
        if self.sandbox is None:
            raise RuntimeError("sandbox is unavailable")
        reason = "close_live" if self.workspace_mode == WorkspaceMode.LIVE else "discard"
        self.sandbox.destroy(reason)

    def _latest_turn(self):
        if not self.runtime_state.turns:
            return None
        return list(self.runtime_state.turns.values())[-1]

    def _refresh(self) -> None:
        self.runtime_state = replay(self.event_store.read_all(), current_runtime_instance_id=self.event_store.runtime_instance_id)
        self.turn_state = self._latest_turn()

    def _execution_alias(self, execution_id: str) -> str:
        for index, current_id in enumerate(self.runtime_state.executions, 1):
            if current_id == execution_id:
                return f"exec_{index}"
        raise ValueError("unknown execution")

    def _resolve_execution_reference(self, reference: str) -> str | None:
        if reference in self.runtime_state.executions:
            return reference
        for index, execution_id in enumerate(self.runtime_state.executions, 1):
            if reference == f"exec_{index}":
                return execution_id
        return None

    def _successful_execution_aliases(self) -> list[str]:
        aliases = [
            self._execution_alias(execution.execution_id)
            for execution in self.runtime_state.executions.values()
            if execution.session_id == self.runtime_state.session_id
            and execution.status == ToolExecutionStatus.COMPLETED
            and execution.tool_name != "update_plan"
        ]
        return aliases[-10:]

    def _workspace_hash(self) -> str:
        files = {}
        for execution in self.runtime_state.executions.values():
            if execution.status != ToolExecutionStatus.COMPLETED or execution.tool_name not in {"write", "edit"}:
                continue
            path = execution.arguments.get("path")
            if not isinstance(path, str) or execution.result is None:
                continue
            digest = execution.result.metadata.get("sha256") or execution.recovery_metadata.get("expected_after_hash")
            if digest:
                files[path] = digest
        payload = json.dumps(files, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _last_successful_execution(self) -> str | None:
        completed = [
            execution for execution in self.runtime_state.executions.values()
            if execution.status == ToolExecutionStatus.COMPLETED and execution.tool_name != "update_plan"
        ]
        return self._execution_alias(completed[-1].execution_id) if completed else None

    def _progress_fingerprint(self, turn_id: str) -> tuple:
        blockers = tuple(
            (blocker.code, blocker.ids)
            for blocker in completion_blockers(self.runtime_state, turn_id)
        )
        return (
            self.runtime_state.state_version,
            self._workspace_hash(),
            blockers,
            self._last_successful_execution(),
        )

    @staticmethod
    def _action_signature(tool_calls) -> str:
        actions = []
        for call in tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = call.function.arguments
            actions.append({"tool": call.function.name, "arguments": arguments})
        return json.dumps(actions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _event(self, event_type, aggregate_type, aggregate_id, payload, *, turn_id=None, causation_id=None):
        event = self.event_store.append_event(
            event_type,
            aggregate_type,
            aggregate_id,
            payload,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=turn_id,
        )
        self._refresh()
        return event

    def _build_system_message(self) -> dict:
        return {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(
                project_md=self.memory_manager.read(),
                runtime_state=runtime_prompt_projection(self.runtime_state),
                workspace_state=self.workspace_state.render(),
            ),
        }

    def _build_context(self) -> list[dict]:
        turn = self.runtime_state.turns.get(self.runtime_state.active_turn_id)
        goal = turn.goal if turn is not None else (self.checkpoint.goal if self.checkpoint else "")
        completed = [
            execution for execution in self.runtime_state.executions.values()
            if execution.status == ToolExecutionStatus.COMPLETED
        ]
        compact_tool_call_ids = {
            execution.tool_call_id for execution in completed
            if execution.tool_name in {"write", "edit"}
        }
        omit_tool_call_ids = self._control_tool_call_ids_to_omit(turn)
        result = self.compactor.build_context(
            system_message=self._build_system_message(),
            message_records=self.event_store.message_records(),
            checkpoint=self.checkpoint,
            session_id=self.event_store.session_id,
            goal=goal,
            compact_tool_call_ids=compact_tool_call_ids,
            omit_tool_call_ids=omit_tool_call_ids,
        )
        if result.compacted and result.checkpoint is not None:
            previous_seq = self.checkpoint.covers_through_seq if self.checkpoint else 0
            self.checkpoint_store.save(result.checkpoint)
            self.checkpoint = result.checkpoint
            self._event(
                "ContextCompacted",
                "context",
                self.event_store.session_id,
                {
                    "previous_covers_through_seq": previous_seq,
                    "covers_through_seq": result.checkpoint.covers_through_seq,
                    "input_tokens_before": result.input_tokens_before,
                    "input_tokens_after": result.input_tokens_after,
                    "summary_input_tokens": result.summary_input_tokens,
                    "duration_ms": result.duration_ms,
                },
                turn_id=self.runtime_state.active_turn_id,
            )
        return result.messages

    def _control_tool_call_ids_to_omit(self, turn) -> set[str]:
        if turn is None:
            return set()
        controls = [
            execution for execution in self.runtime_state.executions.values()
            if execution.turn_id == turn.turn_id and execution.tool_name == "update_plan"
        ]
        keep_failed_id = (
            controls[-1].execution_id
            if controls and controls[-1].status == ToolExecutionStatus.FAILED
            else None
        )
        return {
            execution.tool_call_id for execution in controls
            if execution.execution_id != keep_failed_id
        }

    def _run_with_trace(self, turn_id: str, action):
        filename = f"{turn_id}.txt"
        working_path = self.state_root / "traces" / self.event_store.session_id / filename
        final_path = self.workdir / ".llm-traces" / self.event_store.session_id / filename
        llm.start_trace(working_path)
        try:
            return action()
        finally:
            self._refresh()
            if self.runtime_state.active_turn_id != turn_id:
                saved_path = llm.finish_trace()
                if saved_path is not None:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved_path, final_path)
                    final_path.chmod(0o600)
                    saved_path.unlink()
                    print(f"[trace] saved: {final_path}", flush=True)

    def _consume_stream(self, response, *, emit_text: bool = True):
        full_text = ""
        calls = {}
        started_printing = False
        reported_tool_bytes = 0
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                if emit_text and not started_printing:
                    print("\nassistant> ", end="", flush=True)
                    started_printing = True
                if emit_text:
                    print(content, end="", flush=True)
                full_text += content
            for part in getattr(delta, "tool_calls", None) or []:
                index = getattr(part, "index", None)
                index = 0 if index is None else index
                accumulated = calls.setdefault(index, StreamedToolCall())
                if getattr(part, "id", None):
                    accumulated.id = part.id
                if getattr(part, "type", None):
                    accumulated.type = part.type
                function = getattr(part, "function", None)
                if function is not None:
                    accumulated.function.name += getattr(function, "name", None) or ""
                    accumulated.function.arguments += getattr(function, "arguments", None) or ""
            tool_bytes = sum(len(call.function.arguments.encode("utf-8")) for call in calls.values())
            if tool_bytes - reported_tool_bytes >= 8 * 1024:
                reported_tool_bytes = tool_bytes
                names = ", ".join(call.function.name or "tool" for call in calls.values())
                print(f"[model] receiving tool call {names}: {tool_bytes / 1024:.1f} KiB", flush=True)
        if started_printing:
            print()
        return full_text, [calls[index] for index in sorted(calls)]

    def run_turn(
        self,
        user_input: str,
        image_paths: list[str] | None = None,
        max_iterations: int = 20,
        plan_mode: PlanMode = PlanMode.OPTIONAL,
        resumes_turn_id: str | None = None,
    ) -> str:
        if self.runtime_state.active_turn_id is not None:
            raise RuntimeError("An active turn must be resumed or recovered before starting another turn.")
        turn_id = str(uuid4())
        self.event_store.append(
            "user", build_user_content(user_input, image_paths), turn_id=turn_id
        )
        self._event(
            "TurnStarted",
            "turn",
            turn_id,
            {
                "goal": user_input,
                "plan_mode": PlanMode(plan_mode).value,
                "resumes_turn_id": resumes_turn_id,
            },
            turn_id=turn_id,
        )
        return self._run_with_trace(turn_id, lambda: self._drive_turn(turn_id, max_iterations))

    def pending_runtime_actions(self):
        return pending_runtime_actions(self.runtime_state)

    def resolve_approval(self, execution_id: str, approved: bool, note: str) -> None:
        state = self.runtime_state
        execution = state.executions.get(execution_id)
        if execution is None:
            raise ValueError("unknown execution")
        result = self.tool_executor.resolve_approval(execution_id, approved, note)
        self.event_store.append(
            "tool",
            tool_message_content(result, self._execution_alias(execution_id)),
            tool_call_id=execution.tool_call_id,
            turn_id=execution.turn_id,
        )
        self._refresh()

    def resolve_recovery(self, execution_id: str, decision: RecoveryDecision, note: str) -> None:
        self.tool_executor.resolve_recovery(execution_id, decision, note)
        self._refresh()

    def skip_plan_step(self, step_id: str, reason: str, *, actor: str = "caller") -> None:
        if not reason.strip():
            raise ValueError("skipping a plan step requires a reason")
        plan_id = self.runtime_state.active_plan_id
        if plan_id is None:
            raise ValueError("no active plan")
        plan = self.runtime_state.plans[plan_id]
        step = next((item for item in plan.revisions[-1].steps if item.step_id == step_id), None)
        if step is None:
            raise ValueError(f"unknown plan step: {step_id}")
        self._event(
            "PlanStepSkipped", "plan", plan_id,
            {"step_id": step_id, "note": reason, "actor": actor},
            turn_id=self.runtime_state.active_turn_id,
        )

    def _drain_pending_executions(self, turn_id: str) -> bool:
        pending = [
            execution for execution in self.runtime_state.executions.values()
            if execution.turn_id == turn_id and execution.status == ToolExecutionStatus.PENDING
        ]
        for execution in pending:
            result = self.tool_executor.execute(execution.execution_id)
            self._refresh()
            current = self.runtime_state.executions[execution.execution_id]
            if current.status == ToolExecutionStatus.WAITING_APPROVAL:
                return False
            self.event_store.append(
                "tool", tool_message_content(result, self._execution_alias(execution.execution_id)),
                tool_call_id=execution.tool_call_id,
                turn_id=turn_id,
            )
            self._refresh()
        return True

    def resume_active_turn(self, max_iterations: int = 20) -> str:
        turn_id = self.runtime_state.active_turn_id
        if turn_id is None:
            raise RuntimeError("No active turn to resume")
        if self.pending_runtime_actions():
            raise RuntimeError("Resolve pending approval or recovery before resuming")
        return self._run_with_trace(
            turn_id, lambda: self._resume_active_turn(turn_id, max_iterations)
        )

    def _resume_active_turn(self, turn_id: str, max_iterations: int) -> str:
        if not self._drain_pending_executions(turn_id):
            return ""
        final_events = [
            event for event in self.event_store.read_all()
            if event.get("event_type") == "AssistantMessageRecorded"
            and event.get("turn_id") == turn_id
            and event.get("payload", {}).get("final")
        ]
        if final_events:
            final_text = final_events[-1]["payload"].get("content", "")
            self._event("TurnCompleted", "turn", turn_id, {"final_text": final_text}, turn_id=turn_id)
            return final_text
        return self._drive_turn(turn_id, max_iterations)

    def _drive_turn(self, turn_id: str, max_iterations: int) -> str:
        final_text = ""
        previous_progress = None
        previous_action = None
        for _ in range(max_iterations):
            turn = self.runtime_state.turns[turn_id]
            self._event(
                "TurnIterationAdvanced", "turn", turn_id,
                {"iteration": turn.iteration + 1}, turn_id=turn_id,
            )
            try:
                print(f"[model] waiting for response (iteration {turn.iteration + 1})...", flush=True)
                response = llm.complete(
                    messages=self._build_context(),
                    tools=self.tool_registry.schemas() + [self.plan_tool.schema(self.runtime_state)],
                    model=self.model,
                    stream=True,
                )
                enforced = self.runtime_state.turns[turn_id].plan_mode == PlanMode.REQUIRED or self.runtime_state.turns[turn_id].plan_id is not None
                response_text, tool_calls = self._consume_stream(response, emit_text=not enforced)
            except KeyboardInterrupt:
                print("\n[cancelled by user]", flush=True)
                self._event("TurnInterrupted", "turn", turn_id, {"reason": "user"}, turn_id=turn_id)
                return final_text
            except Exception as error:
                self._event(
                    "TurnFailed", "turn", turn_id,
                    {"error": {"category": "model", "message": str(error), "exception_type": type(error).__name__}},
                    turn_id=turn_id,
                )
                raise

            if tool_calls:
                progress = self._progress_fingerprint(turn_id)
                action = self._action_signature(tool_calls)
                if action == previous_action and progress == previous_progress:
                    self._event(
                        "TurnFailed", "turn", turn_id,
                        {"error": {
                            "category": "stagnation_detected",
                            "message": "Repeated the same action without state or workspace progress",
                        }},
                        turn_id=turn_id,
                    )
                    return ""
                previous_progress = progress
                previous_action = action
                self.event_store.append(
                    "assistant",
                    response_text or None,
                    tool_calls=[call.model_dump() for call in tool_calls],
                    turn_id=turn_id,
                )
                execution_ids = self.tool_executor.request_batch(tool_calls, turn_id)
                self._refresh()
                interrupted = False
                for tool_call, execution_id in zip(tool_calls, execution_ids):
                    if interrupted:
                        self._event(
                            "ToolCancelled", "tool_execution", execution_id,
                            {"reason": "turn interrupted before execution"}, turn_id=turn_id,
                        )
                        self.event_store.append("tool", "Cancelled by user (not executed)", tool_call_id=tool_call.id, turn_id=turn_id)
                        continue
                    print(f"\ntool> {tool_call.function.name}", flush=True)
                    try:
                        result = self.tool_executor.execute(execution_id)
                    except KeyboardInterrupt:
                        interrupted = True
                        self._refresh()
                        execution = self.runtime_state.executions[execution_id]
                        if execution.status.value == "recovery_required":
                            pass
                        elif execution.status.value == "running":
                            self._event(
                                "ToolRecoveryRequired", "tool_execution", execution_id,
                                {"reason": "interrupted with unknown outcome"}, turn_id=turn_id,
                            )
                        self.event_store.append("tool", "Cancelled by user; outcome requires recovery", tool_call_id=tool_call.id, turn_id=turn_id)
                        continue
                    self._refresh()
                    execution = self.runtime_state.executions[execution_id]
                    if execution.status == ToolExecutionStatus.WAITING_APPROVAL:
                        return ""
                    self.event_store.append(
                        "tool",
                        tool_message_content(result, self._execution_alias(execution_id)),
                        tool_call_id=tool_call.id,
                        turn_id=turn_id,
                    )
                    self._refresh()
                if interrupted:
                    self._event("TurnInterrupted", "turn", turn_id, {"reason": "user"}, turn_id=turn_id)
                    return final_text
                continue

            final_text = response_text
            self._event(
                "CompletionRequested", "turn", turn_id,
                {"candidate_text": final_text}, turn_id=turn_id,
            )
            blockers = completion_blockers(self.runtime_state, turn_id)
            if blockers:
                self._event(
                    "CompletionBlocked", "turn", turn_id,
                    {"blockers": [{"code": item.code, "ids": list(item.ids)} for item in blockers]},
                    turn_id=turn_id,
                )
                if self.turn_state.completion_block_count >= 3:
                    self._event(
                        "TurnFailed", "turn", turn_id,
                        {"error": {"category": "repeated_incomplete_plan", "message": "Completion blocked three consecutive times"}},
                        turn_id=turn_id,
                    )
                    return ""
                continue
            self.event_store.append("assistant", final_text, turn_id=turn_id, final=True)
            self._event("TurnCompleted", "turn", turn_id, {"final_text": final_text}, turn_id=turn_id)
            if enforced and final_text:
                print(f"\nassistant> {final_text}", flush=True)
            break
        else:
            self._event(
                "TurnFailed", "turn", turn_id,
                {"error": {"category": "max_iterations", "message": f"Reached max_iterations ({max_iterations})"}},
                turn_id=turn_id,
            )

        return final_text

    def _apply_plan_action(self, *, execution_id: str, turn_id: str, action: str, **arguments) -> ToolResult:
        turn = self.runtime_state.turns[turn_id]
        plan = self.runtime_state.plans.get(turn.plan_id) if turn.plan_id else None
        before = self.runtime_state.state_version

        def envelope(outcome: str, delta: dict | None = None, error: str | None = None, success: bool = True):
            payload = {
                "outcome": outcome,
                "state_version_before": before,
                "state_version_after": self.runtime_state.state_version,
                "delta": delta or {},
                "current_state": runtime_state_frame(self.runtime_state),
            }
            if error:
                payload["error"] = error
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            return ToolResult(content, content, success, metadata={"transition": payload})

        try:
            if action == "create_plan":
                if plan is not None:
                    raise ValueError("an active plan already exists")
                requested_steps = arguments.get("steps")
                if not isinstance(requested_steps, list) or not requested_steps:
                    raise ValueError("create_plan requires at least one plan step")
                steps = []
                for index, item in enumerate(requested_steps, 1):
                    if not isinstance(item, dict):
                        raise ValueError("each plan step must be an object")
                    task = str(item.get("task", "")).strip()
                    if not task:
                        raise ValueError("each plan step requires a task")
                    steps.append({
                        "step_id": f"step_{index}",
                        "task": task,
                        "status": "pending",
                        "required": bool(item.get("required", True)),
                        "completion_policy": CompletionPolicy(item.get("completion_policy", "self_attested")).value,
                        "note": None,
                        "evidence_execution_ids": [],
                    })
                plan_id = str(uuid4())
                self._event(
                    "PlanCreated", "plan", plan_id,
                    {"mode": turn.plan_mode.value, "actor": "model", "reason": arguments.get("reason") or "created", "steps": steps},
                    turn_id=turn_id, causation_id=execution_id,
                )
                return envelope("applied", {"plan": {"from": None, "to": plan_id}})

            if plan is None:
                raise ValueError("no active plan")
            if action in {"complete_step", "fail_step"}:
                step_id = arguments.get("step_id")
                step = next((item for item in plan.revisions[-1].steps if item.step_id == step_id), None)
                if step is None:
                    valid = ", ".join(item.step_id for item in plan.revisions[-1].steps)
                    raise ValueError(f"unknown plan step: {step_id}; valid: {valid}")
                target = PlanStepStatus.COMPLETED if action == "complete_step" else PlanStepStatus.FAILED
                if step.status == target:
                    return envelope("noop")
                if step.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
                    raise ValueError(f"step {step_id} is already {step.status.value}")
                note_key = "note" if action == "complete_step" else "reason"
                note = str(arguments.get(note_key, "")).strip() or None
                evidence = list(arguments.get("evidence_execution_ids", []))
                if action == "complete_step":
                    if step.completion_policy == CompletionPolicy.SELF_ATTESTED and not note:
                        raise ValueError("self-attested completion requires a note")
                    if step.completion_policy == CompletionPolicy.EVIDENCE_REQUIRED:
                        if not evidence:
                            raise ValueError("evidence-required completion needs execution evidence")
                        resolved_evidence = []
                        for evidence_reference in evidence:
                            evidence_id = self._resolve_execution_reference(evidence_reference)
                            found = self.runtime_state.executions.get(evidence_id) if evidence_id else None
                            if found is None or found.session_id != self.runtime_state.session_id or found.status != ToolExecutionStatus.COMPLETED:
                                valid = ", ".join(self._successful_execution_aliases())
                                suffix = f"; valid successful aliases: {valid}" if valid else ""
                                raise ValueError(
                                    f"invalid evidence execution: {evidence_reference}{suffix}"
                                )
                            resolved_evidence.append(evidence_id)
                        evidence = resolved_evidence
                if action == "fail_step" and not note:
                    raise ValueError("failed plan step requires a reason")
                active = [
                    item.step_id for item in plan.revisions[-1].steps
                    if item.status == PlanStepStatus.IN_PROGRESS and item.step_id != step_id
                ]
                if step.status != PlanStepStatus.IN_PROGRESS and active:
                    raise ValueError(f"only one plan step can be in_progress; active: {active[0]}")
                source = step.status.value
                if step.status != PlanStepStatus.IN_PROGRESS:
                    self._event(
                        "PlanStepStarted", "plan", plan.plan_id,
                        {"step_id": step_id}, turn_id=turn_id, causation_id=execution_id,
                    )
                self._event(
                    "PlanStepCompleted" if action == "complete_step" else "PlanStepFailed",
                    "plan", plan.plan_id,
                    {"step_id": step_id, "note": note, "evidence_execution_ids": evidence},
                    turn_id=turn_id, causation_id=execution_id,
                )
                return envelope("applied", {step_id: {"from": source, "to": target.value}})

            if action == "revise_plan":
                reason = str(arguments.get("reason", "")).strip()
                patches = arguments.get("patches")
                if not reason or not isinstance(patches, list) or not patches:
                    raise ValueError("revise_plan requires reason and patches")
                current = {step.step_id: step for step in plan.revisions[-1].steps}
                new_steps = []
                patch_by_id = {}
                for patch in patches:
                    if not isinstance(patch, dict):
                        raise ValueError("each plan patch must be an object")
                    step_id = str(patch.get("step_id", "")).strip()
                    if step_id not in current:
                        raise ValueError(f"unknown plan step: {step_id}")
                    if step_id in patch_by_id:
                        raise ValueError(f"duplicate plan step ID: {step_id}")
                    if set(patch) == {"step_id"}:
                        raise ValueError("plan patch must change at least one field")
                    patch_by_id[step_id] = patch
                for step_id, previous in current.items():
                    patch = patch_by_id.get(step_id, {})
                    task = str(patch.get("task", previous.task)).strip()
                    required = bool(patch.get("required", previous.required))
                    if previous.required and not required:
                        raise ValueError("model cannot downgrade required work")
                    policy_value = patch.get("completion_policy", previous.completion_policy.value)
                    try:
                        completion_policy = CompletionPolicy(policy_value).value
                    except ValueError as error:
                        raise ValueError(f"invalid completion policy: {policy_value}") from error
                    new_steps.append({
                        "step_id": step_id,
                        "task": task,
                        "status": previous.status.value,
                        "required": required,
                        "completion_policy": completion_policy,
                        "note": previous.note,
                        "evidence_execution_ids": list(previous.evidence_execution_ids),
                    })
                if all(
                    item["task"] == current[item["step_id"]].task
                    and item["required"] == current[item["step_id"]].required
                    and item["completion_policy"] == current[item["step_id"]].completion_policy.value
                    for item in new_steps
                ):
                    return envelope("noop")
                self._event(
                    "PlanRevised", "plan", plan.plan_id,
                    {"from_revision": plan.active_revision, "to_revision": plan.active_revision + 1, "actor": "model", "reason": reason, "steps": new_steps},
                    turn_id=turn_id, causation_id=execution_id,
                )
                return envelope("applied", {"plan_revision": {"from": plan.active_revision, "to": plan.active_revision + 1}})
            raise ValueError(f"unknown plan action: {action}")
        except (KeyError, TypeError, ValueError) as error:
            return envelope("rejected", error=str(error), success=False)

    def _execute_tool_call(self, tool_call) -> ToolResult:
        turn_id = self.runtime_state.active_turn_id
        if turn_id is None:
            raise RuntimeError("No active turn")
        execution_id = self.tool_executor.request_batch([tool_call], turn_id)[0]
        result = self.tool_executor.execute(execution_id)
        self._refresh()
        return result
