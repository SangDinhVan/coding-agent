from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from context.checkpoint import CheckpointCorruptionError, CompactionCheckpoint
from model import llm


NARRATIVE_FIELDS = {
    "goal",
    "progress",
    "decisions",
    "constraints",
    "blockers",
    "remaining_work",
    "critical_references",
    "verification",
}

SUMMARY_PROMPT_TEMPLATE = """\
Bạn đang tạo checkpoint bàn giao có cấu trúc cho một coding agent.
Hãy hợp nhất checkpoint trước đó (nếu có) với các message journal mới bên dưới.
Chỉ trả JSON có đúng các key: goal, progress, decisions, constraints, blockers,
remaining_work, critical_references, verification. goal là string; các field còn
lại là array of strings. Không thêm markdown hoặc text ngoài JSON.

--- Goal hiện tại ---
{goal}

--- Checkpoint trước đó ---
{previous_checkpoint}

--- Message journal mới cần compact ---
{conversation}
--- Hết dữ liệu ---
"""


class CompactionError(RuntimeError):
    pass


class ContextBudgetExceeded(CompactionError):
    pass


@dataclass(frozen=True)
class ContextBudget:
    context_window: int
    reserved_output_tokens: int = 16_000
    reserved_tool_tokens: int = 16_000
    safety_margin_tokens: int = 8_000
    checkpoint_max_tokens: int = 8_000
    recent_user_max_tokens: int = 20_000

    def __post_init__(self) -> None:
        values = (
            self.context_window,
            self.reserved_output_tokens,
            self.reserved_tool_tokens,
            self.safety_margin_tokens,
            self.checkpoint_max_tokens,
            self.recent_user_max_tokens,
        )
        if any(not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("context budget values must be non-negative integers")
        if self.available_input_tokens <= 0:
            raise ValueError("context reserves leave no input capacity")

    @property
    def available_input_tokens(self) -> int:
        return (
            self.context_window
            - self.reserved_output_tokens
            - self.reserved_tool_tokens
            - self.safety_margin_tokens
        )


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict]
    checkpoint: CompactionCheckpoint | None
    compacted: bool
    input_tokens_before: int
    input_tokens_after: int
    summary_input_tokens: int
    duration_ms: int


class Compactor:
    def __init__(
        self,
        model: Optional[str] = None,
        budget: ContextBudget | None = None,
    ):
        self.model = model
        self.budget = budget or ContextBudget(context_window=llm.get_context_window(model))

    def should_compact(self, messages: list[dict]) -> bool:
        return self._messages_tokens(messages) > self.budget.available_input_tokens

    def compact(self, messages: list[dict]) -> list[dict]:
        """Compatibility path until Agent is wired to durable checkpoints."""
        if not self.should_compact(messages):
            return messages
        system_messages = [message for message in messages if message.get("role") == "system"]
        non_system = [message for message in messages if message.get("role") != "system"]
        system = system_messages[0] if system_messages else {"role": "system", "content": ""}
        records = list(enumerate(non_system, 1))
        return self.build_context(
            system_message=system,
            message_records=records,
            checkpoint=None,
            session_id="legacy",
            goal="Continue the current task",
        ).messages

    def build_context(
        self,
        *,
        system_message: dict,
        message_records: list[tuple[int, dict]],
        checkpoint: CompactionCheckpoint | None,
        session_id: str,
        goal: str,
    ) -> CompactionResult:
        covered = checkpoint.covers_through_seq if checkpoint else 0
        uncovered = [(seq, message) for seq, message in message_records if seq > covered]
        current = [system_message]
        if checkpoint is not None:
            current.append(checkpoint.to_message())
        current.extend(message for _, message in uncovered)
        before = self._messages_tokens(current)

        newest_user = next(
            ((seq, message) for seq, message in reversed(uncovered) if message.get("role") == "user"),
            None,
        )
        if newest_user and self._messages_tokens([newest_user[1]]) > self.budget.available_input_tokens:
            raise ContextBudgetExceeded("current user message exceeds the available input budget")
        if before <= self.budget.available_input_tokens:
            return CompactionResult(current, checkpoint, False, before, before, 0, 0)

        groups = self._interaction_groups(uncovered)
        retained = self._select_raw_suffix(groups, system_message, newest_user)
        prefix_count = len(groups) - len(retained)
        if prefix_count <= 0:
            raise ContextBudgetExceeded("context exceeds budget but has no compactable prefix")
        compacted_records = [record for group in groups[:prefix_count] for record in group]
        cutoff = compacted_records[-1][0]
        new_checkpoint, summary_tokens, duration_ms = self._create_checkpoint(
            checkpoint=checkpoint,
            records=compacted_records,
            session_id=session_id,
            goal=goal,
            cutoff=cutoff,
            max_seq=max([covered, *(seq for seq, _ in message_records)], default=covered),
        )
        if self._messages_tokens([new_checkpoint.to_message()]) > self.budget.checkpoint_max_tokens:
            raise CompactionError("generated checkpoint exceeds checkpoint token allowance")

        rebuilt = [system_message, new_checkpoint.to_message()]
        rebuilt.extend(message for seq, message in message_records if seq > new_checkpoint.covers_through_seq)
        after = self._messages_tokens(rebuilt)
        if after > self.budget.available_input_tokens:
            raise ContextBudgetExceeded("rebuilt context still exceeds available input budget")
        return CompactionResult(
            rebuilt,
            new_checkpoint,
            True,
            before,
            after,
            summary_tokens,
            duration_ms,
        )

    def _select_raw_suffix(
        self,
        groups: list[list[tuple[int, dict]]],
        system_message: dict,
        newest_user: tuple[int, dict] | None,
    ) -> list[list[tuple[int, dict]]]:
        raw_allowance = max(
            0,
            self.budget.available_input_tokens
            - self._messages_tokens([system_message])
            - self.budget.checkpoint_max_tokens,
        )
        newest_seq = newest_user[0] if newest_user else None
        newest_group_index = next(
            (
                index for index, group in enumerate(groups)
                if newest_seq is not None and any(seq == newest_seq for seq, _ in group)
            ),
            len(groups),
        )
        retained_reversed = []
        retained_tokens = 0
        older_user_tokens = 0
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            group_tokens = self._messages_tokens([message for _, message in group])
            group_older_user_tokens = sum(
                self._messages_tokens([message])
                for seq, message in group
                if message.get("role") == "user" and seq != newest_seq
            )
            required = index >= newest_group_index
            if not required and (
                retained_tokens + group_tokens > raw_allowance
                or older_user_tokens + group_older_user_tokens > self.budget.recent_user_max_tokens
            ):
                break
            retained_reversed.append(group)
            retained_tokens += group_tokens
            older_user_tokens += group_older_user_tokens
        retained_reversed.reverse()
        return retained_reversed

    def _create_checkpoint(
        self,
        *,
        checkpoint: CompactionCheckpoint | None,
        records: list[tuple[int, dict]],
        session_id: str,
        goal: str,
        cutoff: int,
        max_seq: int,
    ) -> tuple[CompactionCheckpoint, int, int]:
        previous = (
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if checkpoint is not None else "(none)"
        )
        prompt = SUMMARY_PROMPT_TEMPLATE.format(
            goal=goal,
            previous_checkpoint=previous,
            conversation=self._render_for_summary(records),
        )
        summary_tokens = llm.count_tokens(prompt, self.model)
        started = time.monotonic()
        try:
            response = llm.complete_text(prompt, model=self.model)
            narrative = json.loads(response)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise CompactionError(f"compaction model returned invalid JSON: {error}") from error
        except Exception as error:
            raise CompactionError(f"compaction model failed: {error}") from error
        duration_ms = round((time.monotonic() - started) * 1000)
        if not isinstance(narrative, dict) or set(narrative) != NARRATIVE_FIELDS:
            raise CompactionError("compaction model returned an invalid checkpoint schema")
        data = {
            "schema_version": 1,
            "session_id": session_id,
            "covers_through_seq": cutoff,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            **narrative,
        }
        try:
            result = CompactionCheckpoint.from_dict(data, session_id=session_id, max_seq=max_seq)
        except CheckpointCorruptionError as error:
            raise CompactionError(str(error)) from error
        return result, summary_tokens, duration_ms

    def _interaction_groups(
        self,
        records: list[tuple[int, dict]],
    ) -> list[list[tuple[int, dict]]]:
        groups = []
        index = 0
        while index < len(records):
            record = records[index]
            message = record[1]
            tool_calls = message.get("tool_calls") or []
            if message.get("role") != "assistant" or not tool_calls:
                groups.append([record])
                index += 1
                continue
            expected_ids = {call.get("id") for call in tool_calls}
            group = [record]
            index += 1
            while index < len(records):
                candidate = records[index]
                candidate_message = candidate[1]
                if (
                    candidate_message.get("role") != "tool"
                    or candidate_message.get("tool_call_id") not in expected_ids
                ):
                    break
                group.append(candidate)
                expected_ids.discard(candidate_message.get("tool_call_id"))
                index += 1
                if not expected_ids:
                    break
            groups.append(group)
        return groups

    def _render_for_summary(self, records: list[tuple[int, dict]]) -> str:
        lines = []
        for seq, message in records:
            content = message.get("content") or ""
            if isinstance(content, list):
                content = llm.content_to_text(content)
            if message.get("tool_calls"):
                names = [call.get("function", {}).get("name", "tool") for call in message["tool_calls"]]
                content = f"{content} [tool_calls: {', '.join(names)}]".strip()
            lines.append(f"[seq={seq}] {message['role']}: {content}")
        return "\n".join(lines)

    def _messages_tokens(self, messages: list[dict]) -> int:
        return llm.count_messages_tokens(messages, self.model)
