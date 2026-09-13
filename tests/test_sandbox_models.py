import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import ControlPaths
from sandbox.models import ResourceLimits, SandboxPath, SandboxStatus


class SandboxModelTests(unittest.TestCase):
    def test_sandbox_path_accepts_only_normalized_relative_paths(self):
        self.assertEqual(str(SandboxPath("src/main.py")), "src/main.py")
        for value in ("/etc/passwd", "../secret", "a/../secret", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SandboxPath(value)

    def test_resource_limits_reject_values_over_hard_ceilings(self):
        ResourceLimits()
        with self.assertRaisesRegex(ValueError, "CPU"):
            ResourceLimits(cpus=4.1)
        with self.assertRaisesRegex(ValueError, "memory"):
            ResourceLimits(memory_bytes=4 * 1024**3 + 1)
        with self.assertRaisesRegex(ValueError, "PIDs"):
            ResourceLimits(pids=513)
        with self.assertRaisesRegex(ValueError, "timeout"):
            ResourceLimits(tool_timeout_seconds=601)

    def test_sandbox_status_has_terminal_destroyed_state(self):
        self.assertEqual(SandboxStatus.DESTROYED.value, "destroyed")


class ControlPathsTests(unittest.TestCase):
    def test_creates_private_session_layout_outside_workspace(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as workspace:
            paths = ControlPaths.create(root, "session-1", workspace)
            self.assertTrue(paths.session_dir.is_dir())
            self.assertTrue(paths.workspace.is_dir())
            self.assertFalse(paths.workspace.is_relative_to(Path(workspace)))
            self.assertEqual(stat.S_IMODE(paths.session_dir.stat().st_mode), 0o700)
            self.assertEqual(paths.session_id, "session-1")

    def test_default_root_honors_xdg_state_home(self):
        with tempfile.TemporaryDirectory() as xdg:
            with patch.dict(os.environ, {"XDG_STATE_HOME": xdg}, clear=False):
                self.assertEqual(ControlPaths.default_root(), Path(xdg) / "sang-coding-agent")

    def test_session_id_cannot_escape_state_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(ValueError):
                ControlPaths.create(root, "../escape", workspace)


if __name__ == "__main__":
    unittest.main()

class DefaultStatePathTests(unittest.TestCase):
    def test_chat_and_memory_defaults_live_under_control_state_root(self):
        from memory.event_store import DEFAULT_CHATS_DIR
        from memory.manager import DEFAULT_PROJECT_MD_PATH

        expected = ControlPaths.default_root()
        self.assertEqual(DEFAULT_CHATS_DIR, expected / "chats")
        self.assertEqual(DEFAULT_PROJECT_MD_PATH, expected / "projects" / "default" / "PROJECT.md")

class ExecutionModelTests(unittest.TestCase):
    def test_exec_request_enforces_timeout_ceiling(self):
        from sandbox.models import ExecRequest
        request = ExecRequest("action-1", "echo ok", SandboxPath("src"), 60)
        self.assertEqual(request.timeout_seconds, 60)
        self.assertEqual(str(ExecRequest("action-2", "pwd").cwd), ".")
        with self.assertRaises(ValueError):
            ExecRequest("action-1", "echo ok", SandboxPath("src"), 601)
