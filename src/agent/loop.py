"""Model-driven agent loop with durable runtime lifecycle boundaries."""

from __future__ import annotations

import base64
import mimetypes
import threading
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

from agent.state import WorkspaceState
from context.compactor import Compactor
from memory.event_store import EventStore
from memory.manager import MemoryManager
from model import llm
from runtime.executor import ToolExecutor
from runtime.models import CompletionPolicy, PlanMode, PlanStepStatus, RecoveryDecision, ToolExecutionStatus, TurnStatus
from runtime.reducer import completion_blockers, pending_runtime_actions, replay, runtime_prompt_projection
from tools import registry
from tools.base import ToolResult
from tools.plan import UpdatePlanTool


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

--- PROJECT.md (facts về project) ---
{project_md}

--- Trạng thái runtime hiện tại ---
{runtime_state}

--- Workspace ---
{workspace_state}
"""


class Agent:
    def __init__(
        self,
        events_path: str,
        model: Optional[str] = None,
        workdir: str = ".",
        policy=None,
        approval_handler=None,
    ):
        self.model = model
        self.event_store = EventStore(path=events_path)
        self.memory_manager = MemoryManager(model=model)
        self.compactor = Compactor(model=model)
        self.workspace_state = WorkspaceState(cwd=workdir)
        self.runtime_state = replay(self.event_store.read_all(), current_runtime_instance_id=self.event_store.runtime_instance_id)
        self.turn_state = self._latest_turn()
        self.plan_tool = UpdatePlanTool(self._apply_plan_action)
        self.tool_executor = ToolExecutor(
            self.event_store,
            get_tool=lambda name: self.plan_tool if name == "update_plan" else registry.get_tool(name),
            policy=policy,
            approval_handler=approval_handler,
        )

    def close(self) -> None:
        self.event_store.close()

    def _latest_turn(self):
        if not self.runtime_state.turns:
            return None
        return list(self.runtime_state.turns.values())[-1]

    def _refresh(self) -> None:
        self.runtime_state = replay(self.event_store.read_all(), current_runtime_instance_id=self.event_store.runtime_instance_id)
        self.turn_state = self._latest_turn()

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
        history = [message for message in self.event_store.to_messages() if message["role"] != "system"]
        messages = [self._build_system_message()] + history
        return self.compactor.compact(messages) if self.compactor.should_compact(messages) else messages

    def _consume_stream(self, response, *, emit_text: bool = True):
        full_text = ""
        calls = {}
        started_printing = False
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
        return self._drive_turn(turn_id, max_iterations)

    def pending_runtime_actions(self):
        return pending_runtime_actions(self.runtime_state)

    def resolve_approval(self, execution_id: str, approved: bool, note: str) -> None:
        state = self.runtime_state
        execution = state.executions.get(execution_id)
        if execution is None:
            raise ValueError("unknown execution")
        result = self.tool_executor.resolve_approval(execution_id, approved, note)
        self.event_store.append("tool", result.compact, tool_call_id=execution.tool_call_id, turn_id=execution.turn_id)
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
                "tool", result.compact,
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
        for _ in range(max_iterations):
            turn = self.runtime_state.turns[turn_id]
            self._event(
                "TurnIterationAdvanced", "turn", turn_id,
                {"iteration": turn.iteration + 1}, turn_id=turn_id,
            )
            try:
                response = llm.complete(
                    messages=self._build_context(),
                    tools=registry.get_schemas() + [self.plan_tool.schema()],
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
                    self.event_store.append("tool", result.compact, tool_call_id=tool_call.id, turn_id=turn_id)
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

        if self.turn_state.status == TurnStatus.COMPLETED:
            recent = self._render_recent_for_memory()
            print("[memory] updating PROJECT.md...", flush=True)
            threading.Thread(target=self._update_memory_bg, args=(recent,), daemon=True).start()
        return final_text

    def _apply_plan_action(self, *, execution_id: str, turn_id: str, action: str, **arguments) -> ToolResult:
        turn = self.runtime_state.turns[turn_id]
        plan = self.runtime_state.plans.get(turn.plan_id) if turn.plan_id else None
        try:
            if action == "create":
                if plan is not None:
                    raise ValueError("an active plan already exists")
                requested_steps = arguments.get("steps")
                if not isinstance(requested_steps, list) or not requested_steps:
                    raise ValueError("create requires at least one plan step")
                steps = []
                for index, item in enumerate(requested_steps, 1):
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
                return ToolResult(f"Created plan {plan_id}", f"Created plan with {len(steps)} step(s)", True)

            if plan is None:
                raise ValueError("no active plan")
            if action == "set_step_status":
                step_id = arguments.get("step_id")
                status = arguments.get("status")
                step = next((item for item in plan.revisions[-1].steps if item.step_id == step_id), None)
                if step is None:
                    raise ValueError(f"unknown plan step: {step_id}")
                event_type = {
                    "in_progress": "PlanStepStarted",
                    "completed": "PlanStepCompleted",
                    "failed": "PlanStepFailed",
                }.get(status)
                if event_type is None:
                    raise ValueError("model cannot skip a plan step")
                note = str(arguments.get("note", "")).strip() or None
                evidence = list(arguments.get("evidence_execution_ids", []))
                if status == "completed":
                    if step.status != PlanStepStatus.IN_PROGRESS:
                        raise ValueError("invalid plan step transition: completion requires in_progress")
                    if step.completion_policy == CompletionPolicy.SELF_ATTESTED and not note:
                        raise ValueError("self-attested completion requires a note")
                    if step.completion_policy == CompletionPolicy.EVIDENCE_REQUIRED:
                        if not evidence:
                            raise ValueError("evidence-required completion needs execution evidence")
                        for evidence_id in evidence:
                            found = self.runtime_state.executions.get(evidence_id)
                            if found is None or found.session_id != self.runtime_state.session_id or found.status != ToolExecutionStatus.COMPLETED:
                                raise ValueError(f"invalid evidence execution: {evidence_id}")
                if status == "failed" and not note:
                    raise ValueError("failed plan step requires a reason")
                if status == "in_progress":
                    active = [item.step_id for item in plan.revisions[-1].steps if item.status == PlanStepStatus.IN_PROGRESS and item.step_id != step_id]
                    if active:
                        raise ValueError(f"only one plan step can be in_progress; active: {active[0]}")
                self._event(
                    event_type, "plan", plan.plan_id,
                    {"step_id": step_id, "note": note, "evidence_execution_ids": evidence},
                    turn_id=turn_id, causation_id=execution_id,
                )
                return ToolResult(f"Set {step_id} to {status}", f"Plan step {step_id} is now {status}", True)

            if action == "revise":
                reason = str(arguments.get("reason", "")).strip()
                requested_steps = arguments.get("steps")
                if not reason or not isinstance(requested_steps, list) or not requested_steps:
                    raise ValueError("revise requires reason and steps")
                current = {step.step_id: step for step in plan.revisions[-1].steps}
                new_steps = []
                for index, item in enumerate(requested_steps, 1):
                    step_id = item.get("step_id") or f"step_{index}"
                    previous = current.get(step_id)
                    required = bool(item.get("required", previous.required if previous else True))
                    if previous and previous.required and not required:
                        raise ValueError("model cannot downgrade required work")
                    if previous and previous.status in {PlanStepStatus.COMPLETED, PlanStepStatus.SKIPPED}:
                        status = previous.status.value
                        note = previous.note
                        evidence = previous.evidence_execution_ids
                    else:
                        status = "pending"
                        note = None
                        evidence = []
                    new_steps.append({
                        "step_id": step_id,
                        "task": str(item.get("task", previous.task if previous else "")).strip(),
                        "status": status,
                        "required": required,
                        "completion_policy": (previous.completion_policy.value if previous else item.get("completion_policy", "self_attested")),
                        "note": note,
                        "evidence_execution_ids": list(evidence),
                    })
                missing_required = [step.step_id for step in current.values() if step.required and step.step_id not in {item["step_id"] for item in new_steps}]
                if missing_required:
                    raise ValueError("model cannot remove required work")
                self._event(
                    "PlanRevised", "plan", plan.plan_id,
                    {"from_revision": plan.active_revision, "to_revision": plan.active_revision + 1, "actor": "model", "reason": reason, "steps": new_steps},
                    turn_id=turn_id, causation_id=execution_id,
                )
                return ToolResult("Plan revised", f"Plan revised to revision {plan.active_revision + 1}", True)
            raise ValueError(f"unknown plan action: {action}")
        except (KeyError, TypeError, ValueError) as error:
            return ToolResult(str(error), f"Error: {error}", False)

    def _update_memory_bg(self, recent: str):
        try:
            self.memory_manager.update(recent)
            print(flush=True)
        except Exception as error:
            print(f"[memory] update failed: {error}", flush=True)

    def _execute_tool_call(self, tool_call) -> ToolResult:
        turn_id = self.runtime_state.active_turn_id
        if turn_id is None:
            raise RuntimeError("No active turn")
        execution_id = self.tool_executor.request_batch([tool_call], turn_id)[0]
        result = self.tool_executor.execute(execution_id)
        self._refresh()
        return result

    def _render_recent_for_memory(self, last_n: int = 20) -> str:
        lines = []
        for message in self.event_store.to_messages()[-last_n:]:
            content = message.get("content", "")
            if isinstance(content, list):
                content = llm.content_to_text(content)
            lines.append(f"{message['role']}: {content}")
        return "\n".join(lines)
