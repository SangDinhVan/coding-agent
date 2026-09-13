import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from main import _default_agent_factory, build_parser, handle_pending_runtime_actions, run_repl
from runtime.models import PendingRuntimeAction, RecoveryDecision
from sandbox.models import ResourceLimits, WorkspaceMode


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

class SandboxControlCommandTests(unittest.TestCase):
    def test_changes_is_intercepted_before_model(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        agent.prepare_changes.return_value = MagicMock(
            approvable=True, change_set_hash="abc", violations=(), entries=(),
        )
        inputs = iter(["/changes", "exit"])
        run_repl(Path("session.jsonl"), input_fn=lambda prompt: next(inputs), print_fn=lambda text: None, agent_factory=lambda path: agent)
        agent.prepare_changes.assert_called_once()
        agent.run_turn.assert_not_called()

    def test_apply_requires_exact_hash_and_note(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        agent.last_changes = MagicMock(change_set_hash="abc")
        inputs = iter(["/apply", "approve abc", "reviewed"])
        run_repl(Path("session.jsonl"), input_fn=lambda prompt: next(inputs), print_fn=lambda text: None, agent_factory=lambda path: agent)
        agent.apply_changes.assert_called_once_with("abc", "reviewed")
        agent.run_turn.assert_not_called()

    def test_discard_is_intercepted_and_finalizes(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        run_repl(Path("session.jsonl"), input_fn=lambda prompt: "/discard", print_fn=lambda text: None, agent_factory=lambda path: agent)
        agent.discard.assert_called_once()
        agent.run_turn.assert_not_called()

    def test_live_changes_and_apply_explain_git_workflow_without_finalization(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        agent.workspace_mode = WorkspaceMode.LIVE
        inputs = iter(["/changes", "/apply", "exit"])
        outputs = []

        run_repl(
            Path("session.jsonl"), input_fn=lambda prompt: next(inputs),
            print_fn=outputs.append, agent_factory=lambda path: agent,
        )

        agent.prepare_changes.assert_not_called()
        agent.apply_changes.assert_not_called()
        self.assertTrue(any("git diff" in output for output in outputs))
        self.assertTrue(any("already" in output.lower() for output in outputs))

    def test_live_discard_only_closes_sandbox_and_warns_changes_remain(self):
        agent = MagicMock()
        agent.pending_runtime_actions.return_value = []
        agent.runtime_state.active_turn_id = None
        agent.workspace_mode = WorkspaceMode.LIVE
        outputs = []

        run_repl(
            Path("session.jsonl"), input_fn=lambda prompt: "/discard",
            print_fn=outputs.append, agent_factory=lambda path: agent,
        )

        agent.discard.assert_called_once()
        self.assertTrue(any("remain" in output.lower() for output in outputs))


class SandboxFactoryTests(unittest.TestCase):
    image = "sandbox@sha256:" + "a" * 64

    def test_parser_accepts_workspace_state_root_image_and_resource_limits(self):
        args = build_parser().parse_args([
            "--workspace", "/work", "--state-root", "/state", "--image", self.image,
            "--cpus", "1.5", "--memory-mib", "1024", "--pids", "128",
            "--tmpfs-mib", "256", "--tool-timeout", "30", "--workspace-growth-mib", "512",
        ])
        self.assertEqual(args.workspace, "/work")
        self.assertEqual(args.state_root, "/state")
        self.assertEqual(args.image, self.image)
        self.assertEqual(args.cpus, 1.5)
        self.assertEqual(args.memory_mib, 1024)
        self.assertEqual(args.pids, 128)

    def test_new_session_is_created_started_and_bound_before_agent(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            events = Path(state) / "chats" / "session-1.jsonl"
            paths = MagicMock()
            paths.metadata.exists.return_value = False
            session = MagicMock()
            with patch("main.ControlPaths.create", return_value=paths), patch("main.DockerBackend") as backend_type, patch(
                "main.SandboxSession", return_value=session
            ) as session_type, patch("main.ToolRegistry") as registry_type, patch("main.Agent") as agent_type, patch(
                "main.recover_incomplete_apply", return_value=False
            ) as recover:
                _default_agent_factory(events, Path(workspace), Path(state), self.image, ResourceLimits())
            session.create.assert_called_once_with()
            session.start.assert_called_once_with()
            session.resume.assert_not_called()
            session.start_watchdog.assert_called_once_with()
            registry_type.assert_called_once_with(session)
            agent_type.assert_called_once()
            recover.assert_called_once_with(paths, Path(workspace).resolve())
            self.assertIs(agent_type.call_args.kwargs["sandbox"], session)
            self.assertEqual(session_type.call_args.kwargs["mode"], WorkspaceMode.LIVE)

    def test_existing_stopped_session_restores_exact_container_and_reconciles_before_agent(self):
        from sandbox.models import SandboxStatus

        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            events = Path(state) / "chats" / "session-1.jsonl"
            paths = MagicMock()
            paths.metadata.exists.return_value = True
            paths.metadata.read_text.return_value = '{"container_id":"cid"}'
            backend = MagicMock()
            session = MagicMock(status=SandboxStatus.STOPPED)
            with patch("main.ControlPaths.create", return_value=paths), patch("main.DockerBackend", return_value=backend), patch(
                "main.SandboxSession", return_value=session
            ), patch("main.ToolRegistry"), patch("main.Agent") as agent_type, patch(
                "main.recover_incomplete_apply", return_value=False
            ):
                _default_agent_factory(events, Path(workspace), Path(state), self.image, ResourceLimits())
            self.assertEqual(backend.container_id, "cid")
            session.resume.assert_called_once_with()
            session.start_watchdog.assert_called_once_with()
            session.create.assert_not_called()
            agent_type.assert_called_once()

    def test_existing_sealed_session_is_restored_without_restart(self):
        from sandbox.models import SandboxStatus

        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as state:
            events = Path(state) / "chats" / "session-1.jsonl"
            paths = MagicMock()
            paths.metadata.exists.return_value = True
            paths.metadata.read_text.return_value = '{"container_id":"cid"}'
            session = MagicMock(status=SandboxStatus.SEALED)
            with patch("main.ControlPaths.create", return_value=paths), patch("main.DockerBackend"), patch(
                "main.SandboxSession", return_value=session
            ), patch("main.ToolRegistry"), patch("main.Agent") as agent_type, patch(
                "main.recover_incomplete_apply", return_value=False
            ):
                _default_agent_factory(events, Path(workspace), Path(state), self.image, ResourceLimits())
            session.resume.assert_not_called()
            session.start_watchdog.assert_not_called()
            session.create.assert_not_called()
            agent_type.assert_called_once()
