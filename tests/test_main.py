import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from main import handle_pending_runtime_actions, run_repl
from runtime.models import PendingRuntimeAction, RecoveryDecision


class MainTests(unittest.TestCase):
    def test_manual_bash_recovery_does_not_offer_retry(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = [PendingRuntimeAction("recovery", "execution-1", "bash", "unknown")]
        agent.runtime_state.executions = {"execution-1": MagicMock(replay_policy=MagicMock(value="manual"))}
        choices = iter(["failed", "checked process state"])
        outputs = []
        handle_pending_runtime_actions(agent, input_fn=lambda prompt: next(choices), print_fn=outputs.append)
        agent.resolve_recovery.assert_called_once_with("execution-1", RecoveryDecision.FAILED, "checked process state")
        self.assertFalse(any("retry" in output.lower() for output in outputs))

    def test_active_turn_can_resume_without_new_user_input(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = "t1"
        inputs = iter(["resume", "exit"])
        run_repl(Path("session.jsonl"), input_fn=lambda prompt: next(inputs), print_fn=lambda text: None, agent_factory=lambda path: agent)
        agent.resume_active_turn.assert_called_once()
        agent.run_turn.assert_not_called()

    def test_repl_closes_agent_on_exit(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        run_repl(Path("session.jsonl"), input_fn=lambda prompt: "exit", print_fn=lambda text: None, agent_factory=lambda path: agent)
        agent.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

class ApprovalCliTests(unittest.TestCase):
    def test_waiting_approval_is_resolved_before_repl(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = [PendingRuntimeAction("approval", "execution-1", "write", "policy")]
        choices = iter(["approve", "reviewed"])
        handle_pending_runtime_actions(agent, input_fn=lambda prompt: next(choices), print_fn=lambda text: None)
        agent.resolve_approval.assert_called_once()
