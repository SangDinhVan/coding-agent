import unittest
from unittest.mock import patch

from context.compactor import Compactor


class CompactorTests(unittest.TestCase):
    def test_compaction_never_splits_assistant_tool_calls_from_results(self):
        messages = [
            {"role": "system", "content": "runtime truth"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            *({"role": "user", "content": f"recent-{index}"} for index in range(9)),
        ]
        compactor = Compactor()
        with patch.object(compactor, "should_compact", return_value=True), patch(
            "context.compactor.llm.complete_text", return_value="summary"
        ):
            compacted = compactor.compact(messages)
        call_index = next(index for index, item in enumerate(compacted) if item.get("tool_calls"))
        self.assertEqual(compacted[call_index + 1]["tool_call_id"], "c1")

    def test_runtime_system_projection_is_preserved_verbatim(self):
        runtime = {"role": "system", "content": "active plan and recovery blockers"}
        messages = [runtime] + [{"role": "user", "content": str(index)} for index in range(12)]
        compactor = Compactor()
        with patch.object(compactor, "should_compact", return_value=True), patch(
            "context.compactor.llm.complete_text", return_value="summary"
        ):
            compacted = compactor.compact(messages)
        self.assertIs(compacted[0], runtime)
        self.assertEqual(compacted[0]["content"], "active plan and recovery blockers")

    def test_runtime_only_events_are_not_renderable_messages(self):
        compactor = Compactor()
        rendered = compactor._render_for_summary([
            {"role": "user", "content": "goal"},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
        ])
        self.assertEqual(rendered, "user: goal\ntool: ok")


if __name__ == "__main__":
    unittest.main()
