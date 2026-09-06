import copy
import unittest

from memory.event_store import JournalCorruptionError
from runtime.models import PlanMode, ToolExecutionStatus, TurnStatus
from runtime.reducer import completion_blockers, pending_runtime_actions, replay


def event(seq, event_type, aggregate_type, aggregate_id, payload=None, turn_id=None, session_id="s"):
    return {
        "schema_version": 1,
        "event_id": f"e{seq}",
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}Z",
        "session_id": session_id,
        "runtime_instance_id": "runtime",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "turn_id": turn_id,
        "causation_id": None,
        "correlation_id": turn_id,
        "payload": payload or {},
    }


class ReducerTests(unittest.TestCase):
    def test_same_event_sequence_builds_equal_runtime_state(self):
        events = [
            event(1, "TurnStarted", "turn", "t1", {"goal": "goal", "plan_mode": "optional"}, "t1"),
            event(2, "TurnIterationAdvanced", "turn", "t1", {"iteration": 1}, "t1"),
            event(3, "TurnCompleted", "turn", "t1", {"final_text": "done"}, "t1"),
        ]
        self.assertEqual(replay(copy.deepcopy(events)), replay(copy.deepcopy(events)))
        state = replay(events)
        self.assertEqual(state.turns["t1"].status, TurnStatus.COMPLETED)
        self.assertEqual(state.turns["t1"].iteration, 1)

    def test_legacy_messages_do_not_fabricate_turns(self):
        state = replay([{"seq": 1, "ts": "old", "role": "user", "content": "legacy"}])
        self.assertEqual(state.turns, {})
        self.assertIsNone(state.active_turn_id)

    def test_started_without_terminal_becomes_recovery_required_after_replay(self):
        events = [
            event(1, "TurnStarted", "turn", "t1", {"goal": "g"}, "t1"),
            event(2, "ToolRequested", "tool_execution", "x1", {
                "tool_call_id": "c1", "tool_name": "read", "arguments": {}, "replay_policy": "replay_safe"
            }, "t1"),
            event(3, "ToolValidated", "tool_execution", "x1", {"arguments": {}}, "t1"),
            event(4, "ToolStarted", "tool_execution", "x1", {}, "t1"),
        ]
        state = replay(events)
        self.assertEqual(state.executions["x1"].status, ToolExecutionStatus.RECOVERY_REQUIRED)
        self.assertEqual(pending_runtime_actions(state)[0].kind, "recovery")

    def test_waiting_approval_is_restored(self):
        events = [
            event(1, "TurnStarted", "turn", "t1", {"goal": "g"}, "t1"),
            event(2, "ToolRequested", "tool_execution", "x1", {
                "tool_call_id": "c1", "tool_name": "bash", "arguments": {"command": "echo ok"}
            }, "t1"),
            event(3, "ToolApprovalRequested", "tool_execution", "x1", {"reason": "policy"}, "t1"),
        ]
        state = replay(events)
        self.assertEqual(state.executions["x1"].status, ToolExecutionStatus.WAITING_APPROVAL)
        self.assertEqual(pending_runtime_actions(state)[0].kind, "approval")

    def test_completed_execution_is_never_pending(self):
        events = [
            event(1, "TurnStarted", "turn", "t1", {"goal": "g"}, "t1"),
            event(2, "ToolRequested", "tool_execution", "x1", {
                "tool_call_id": "c1", "tool_name": "read", "arguments": {}
            }, "t1"),
            event(3, "ToolValidated", "tool_execution", "x1", {"arguments": {}}, "t1"),
            event(4, "ToolStarted", "tool_execution", "x1", {}, "t1"),
            event(5, "ToolCompleted", "tool_execution", "x1", {
                "result": {"raw": "r", "compact": "c", "success": True}
            }, "t1"),
        ]
        state = replay(events)
        self.assertEqual(state.executions["x1"].status, ToolExecutionStatus.COMPLETED)
        self.assertEqual(pending_runtime_actions(state), [])

    def test_invalid_historical_transition_reports_seq(self):
        events = [
            event(1, "TurnStarted", "turn", "t1", {"goal": "g"}, "t1"),
            event(2, "TurnCompleted", "turn", "t1", {"final_text": "done"}, "t1"),
            event(3, "TurnFailed", "turn", "t1", {"error": {"category": "x", "message": "bad"}}, "t1"),
        ]
        with self.assertRaisesRegex(JournalCorruptionError, "seq 3"):
            replay(events)

    def test_completion_blockers_are_stable_and_specific(self):
        events = [event(1, "TurnStarted", "turn", "t1", {"goal": "g", "plan_mode": "required"}, "t1")]
        blockers = completion_blockers(replay(events), "t1")
        self.assertEqual(blockers[0].code, "missing_required_plan")


if __name__ == "__main__":
    unittest.main()
