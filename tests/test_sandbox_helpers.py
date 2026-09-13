import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


def load(name):
    path = ROOT / "sandbox-image" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FilesystemHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = load("sandbox_fs")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)

    def call(self, **request):
        return self.fs.handle(request, self.workspace)

    def test_rejects_absolute_parent_and_symlink_paths(self):
        outside = self.workspace.parent / "sandbox-helper-canary"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.workspace / "escape").symlink_to(outside)
        (self.workspace / "escape-dir").symlink_to(outside.parent)
        for path in ("/etc/passwd", "../secret", "escape", "escape-dir/secret"):
            with self.subTest(path=path):
                result = self.call(operation="read", path=path)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "blocked_by_sandbox")

    def test_write_read_edit_and_fingerprint(self):
        result = self.call(operation="write", path="pkg/file.txt", content="before")
        self.assertTrue(result["success"])
        self.assertEqual(self.call(operation="read", path="pkg/file.txt")["content"], "before")
        edited = self.call(operation="edit", path="pkg/file.txt", old_string="before", new_string="after")
        self.assertTrue(edited["success"])
        fingerprint = self.call(operation="fingerprint", path="pkg/file.txt")
        self.assertEqual(len(fingerprint["sha256"]), 64)
        self.assertEqual((self.workspace / "pkg/file.txt").read_text(), "after")

    def test_edit_requires_unique_match(self):
        (self.workspace / "x.txt").write_text("x x", encoding="utf-8")
        result = self.call(operation="edit", path="x.txt", old_string="x", new_string="y")
        self.assertFalse(result["success"])
        self.assertIn("unique", result["error"])

    def test_rejects_special_file(self):
        os.mkfifo(self.workspace / "pipe")
        result = self.call(operation="read", path="pipe")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "blocked_by_sandbox")


class ExecHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executor = load("sandbox_exec")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)

    def test_exec_captures_exit_code_and_bounds_output(self):
        result = self.executor.execute({"command": "printf 123456789", "cwd": ".", "timeout_seconds": 2}, self.workspace, output_limit=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"].encode()), 5)

    def test_exec_rejects_cwd_escape(self):
        result = self.executor.execute({"command": "pwd", "cwd": "../", "timeout_seconds": 2}, self.workspace)
        self.assertEqual(result["status"], "blocked_by_sandbox")

    def test_timeout_kills_process_group(self):
        marker = self.workspace / "survived"
        command = f"(sleep 1; touch {marker}) & sleep 30"
        started = time.monotonic()
        result = self.executor.execute({"command": command, "cwd": ".", "timeout_seconds": 0.1}, self.workspace)
        self.assertEqual(result["status"], "timed_out")
        self.assertLess(time.monotonic() - started, 2)
        time.sleep(1.1)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
