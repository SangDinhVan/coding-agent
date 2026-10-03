import hashlib
import json
import unittest
from unittest.mock import patch

from context.checkpoint import CompactionCheckpoint
from context.compactor import (
    CompactionError,
    Compactor,
    ContextBudget,
    ContextBudgetExceeded,
)


NARRATIVE = {
    "goal": "Continue the task",
    "progress": ["old work summarized"],
    "decisions": [],
    "constraints": [],
    "blockers": [],
    "remaining_work": ["finish"],
    "critical_references": [],
    "verification": [],
}


def checkpoint(covers=10):
    return CompactionCheckpoint.from_dict({
        "schema_version": 1,
        "session_id": "s",
        "covers_through_seq": covers,
        "created_at": "2026-09-29T10:00:00Z",
        **NARRATIVE,
    }, session_id="s", max_seq=100)


def fake_message_tokens(messages, model=None):
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if str(content).startswith("[Compaction checkpoint]"):
            total += 20
        else:
            total += len(str(content))
        total += 10 * len(message.get("tool_calls") or [])
    return total


def small_budget(context_window, **changes):
    return ContextBudget(
        context_window=context_window,
        reserved_output_tokens=0,
        reserved_tool_tokens=0,
        safety_margin_tokens=0,
        **changes,
    )


class ContextBudgetTests(unittest.TestCase):
    def test_available_input_subtracts_global_reserves(self):
        budget = ContextBudget(
            context_window=100_000,
            reserved_output_tokens=16_000,
            reserved_tool_tokens=16_000,
            safety_margin_tokens=8_000,
        )
        self.assertEqual(budget.available_input_tokens, 60_000)

    def test_rejects_reserves_that_leave_no_input_capacity(self):
        with self.assertRaises(ValueError):
            ContextBudget(
                context_window=10,
                reserved_output_tokens=10,
                reserved_tool_tokens=0,
                safety_margin_tokens=0,
            )


class CompactorTests(unittest.TestCase):
    def build(
        self,
        records,
        *,
        budget=None,
        previous=None,
        counter=fake_message_tokens,
        compact_tool_call_ids=None,
        omit_tool_call_ids=None,
    ):
        compactor = Compactor(budget=budget or small_budget(100))
        with (
            patch("context.compactor.llm.count_messages_tokens", side_effect=counter),
            patch("context.compactor.llm.count_tokens", side_effect=lambda text, model=None: len(text)),
            patch("context.compactor.llm.complete_text", return_value=json.dumps(NARRATIVE)) as complete,
        ):
            result = compactor.build_context(
                system_message={"role": "system", "content": "sys"},
                message_records=records,
                checkpoint=previous,
                session_id="s",
                goal="Continue the task",
                compact_tool_call_ids=compact_tool_call_ids,
                omit_tool_call_ids=omit_tool_call_ids,
            )
        return result, complete

    def test_below_budget_returns_original_messages_without_summary_call(self):
        system = {"role": "system", "content": "runtime truth"}
        records = [(1, {"role": "user", "content": "goal"})]
        compactor = Compactor(budget=small_budget(1000))
        with patch("context.compactor.llm.count_messages_tokens", side_effect=fake_message_tokens), patch(
            "context.compactor.llm.complete_text"
        ) as complete:
            result = compactor.build_context(
                system_message=system, message_records=records, checkpoint=None,
                session_id="s", goal="goal",
            )
        self.assertFalse(result.compacted)
        self.assertIs(result.messages[0], system)
        self.assertEqual(result.messages[1:], [records[0][1]])
        complete.assert_not_called()

    def test_completed_large_write_is_projected_without_mutating_journal_message(self):
        body = "x" * 12_000
        arguments = json.dumps({"path": "output/index.html", "content": body})
        records = [
            (1, {"role": "user", "content": "build it"}),
            (2, {"role": "assistant", "tool_calls": [{
                "id": "write-1",
                "type": "function",
                "function": {"name": "write", "arguments": arguments},
            }]}),
            (3, {"role": "tool", "tool_call_id": "write-1", "content": "Successfully wrote output/index.html"}),
        ]

        result, _ = self.build(
            records,
            budget=small_budget(100_000),
            compact_tool_call_ids={"write-1"},
        )

        projected = json.loads(result.messages[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(projected["path"], "output/index.html")
        self.assertEqual(
            projected["content"],
            f"[omitted: 12000 bytes, sha256={hashlib.sha256(body.encode()).hexdigest()}]",
        )
        self.assertEqual(records[1][1]["tool_calls"][0]["function"]["arguments"], arguments)

    def test_runtime_represented_plan_calls_are_omitted_as_complete_groups(self):
        records = [
            (1, {"role": "user", "content": "build it"}),
            (2, {"role": "assistant", "tool_calls": [{
                "id": "plan-1",
                "type": "function",
                "function": {"name": "update_plan", "arguments": '{"action":"create","steps":[{"task":"work"}]}'},
            }]}),
            (3, {"role": "tool", "tool_call_id": "plan-1", "content": "Created 1 pending step(s): step_1"}),
            (4, {"role": "assistant", "content": "continuing"}),
        ]

        result, _ = self.build(
            records,
            budget=small_budget(100_000),
            omit_tool_call_ids={"plan-1"},
        )

        self.assertEqual(result.messages[1:], [records[0][1], records[3][1]])

    def test_newest_user_is_preserved_and_older_user_budget_is_enforced(self):
        records = [
            (1, {"role": "assistant", "content": "x" * 90}),
            (2, {"role": "user", "content": "aaaa"}),
            (3, {"role": "user", "content": "bbbb"}),
            (4, {"role": "user", "content": "cccc"}),
        ]
        budget = small_budget(
            context_window=100, checkpoint_max_tokens=20, recent_user_max_tokens=5
        )
        result, _ = self.build(records, budget=budget)
        raw_users = [message["content"] for message in result.messages if message["role"] == "user"]
        self.assertEqual(raw_users[-1], "cccc")
        self.assertLessEqual(sum(len(value) for value in raw_users[:-1]), 5)

    def test_newest_user_larger_than_available_budget_fails_without_truncation(self):
        compactor = Compactor(budget=small_budget(100))
        with patch("context.compactor.llm.count_messages_tokens", side_effect=fake_message_tokens):
            with self.assertRaises(ContextBudgetExceeded):
                compactor.build_context(
                    system_message={"role": "system", "content": "sys"},
                    message_records=[(1, {"role": "user", "content": "u" * 101})],
                    checkpoint=None, session_id="s", goal="goal",
                )

    def test_tool_call_and_all_results_are_compacted_as_one_group(self):
        records = [
            (1, {"role": "user", "content": "o" * 30}),
            (2, {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "read"}},
                {"id": "c2", "function": {"name": "read"}},
            ]}),
            (3, {"role": "tool", "tool_call_id": "c1", "content": "a" * 20}),
            (4, {"role": "tool", "tool_call_id": "c2", "content": "b" * 20}),
            (5, {"role": "user", "content": "n" * 20}),
        ]
        budget = small_budget(100, checkpoint_max_tokens=30)
        result, _ = self.build(records, budget=budget)
        self.assertEqual(result.checkpoint.covers_through_seq, 4)
        self.assertFalse(any(message.get("tool_calls") for message in result.messages))
        self.assertFalse(any(message.get("role") == "tool" for message in result.messages))

    def test_incremental_summary_uses_previous_checkpoint_and_only_new_prefix(self):
        records = [
            (11, {"role": "user", "content": "old-eleven-" + "x" * 30}),
            (12, {"role": "assistant", "content": "keep-twelve-" + "y" * 20}),
            (13, {"role": "user", "content": "keep-thirteen"}),
        ]
        budget = small_budget(100, checkpoint_max_tokens=30)
        result, complete = self.build(records, budget=budget, previous=checkpoint(10))
        prompt = complete.call_args.args[0]
        self.assertIn('"covers_through_seq":10', prompt)
        self.assertIn("[seq=11]", prompt)
        self.assertNotIn("[seq=12]", prompt)
        self.assertNotIn("[seq=13]", prompt)
        self.assertEqual(result.checkpoint.covers_through_seq, 11)

    def test_invalid_summary_json_or_schema_fails(self):
        records = [
            (1, {"role": "assistant", "content": "x" * 100}),
            (2, {"role": "user", "content": "current"}),
        ]
        compactor = Compactor(budget=small_budget(80, checkpoint_max_tokens=30))
        for response in ("not-json", json.dumps({"goal": "missing lists"})):
            with self.subTest(response=response), patch(
                "context.compactor.llm.count_messages_tokens", side_effect=fake_message_tokens
            ), patch("context.compactor.llm.count_tokens", return_value=10), patch(
                "context.compactor.llm.complete_text", return_value=response
            ):
                with self.assertRaises(CompactionError):
                    compactor.build_context(
                        system_message={"role": "system", "content": "sys"},
                        message_records=records, checkpoint=None, session_id="s", goal="goal",
                    )

    def test_checkpoint_over_allowance_fails(self):
        records = [
            (1, {"role": "assistant", "content": "x" * 100}),
            (2, {"role": "user", "content": "current"}),
        ]

        def oversized_checkpoint(messages, model=None):
            if len(messages) == 1 and str(messages[0].get("content", "")).startswith("[Compaction checkpoint]"):
                return 31
            return fake_message_tokens(messages, model)

        with self.assertRaises(CompactionError):
            self.build(
                records,
                budget=small_budget(80, checkpoint_max_tokens=30),
                counter=oversized_checkpoint,
            )

    def test_rebuilt_context_over_budget_fails_without_dropping_raw_messages(self):
        records = [
            (1, {"role": "assistant", "content": "x" * 100}),
            (2, {"role": "user", "content": "current"}),
        ]

        def inconsistent_counter(messages, model=None):
            if len(messages) > 1 and any(
                str(message.get("content", "")).startswith("[Compaction checkpoint]")
                for message in messages
            ):
                return 200
            return fake_message_tokens(messages, model)

        with self.assertRaises(ContextBudgetExceeded):
            self.build(
                records,
                budget=small_budget(80, checkpoint_max_tokens=30),
                counter=inconsistent_counter,
            )


if __name__ == "__main__":
    unittest.main()
