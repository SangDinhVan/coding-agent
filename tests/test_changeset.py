import os
import tempfile
import unittest
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import ChangeSetError, build_changeset, verify_sealed_changeset
from sandbox.workspace import prepare_workspace


class ChangeSetTests(unittest.TestCase):
    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.addCleanup(self.state_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        self.paths = ControlPaths.create(self.state_tmp.name, "s1", self.source)

    def snapshot(self):
        return prepare_workspace(self.source, self.paths, free_space_floor_bytes=0)

    def test_classifies_create_modify_delete_binary_and_mode_deterministically(self):
        (self.source / "modify.txt").write_text("before\n")
        (self.source / "delete.txt").write_text("delete\n")
        (self.source / "mode.sh").write_text("echo ok\n")
        self.snapshot()
        (self.paths.workspace / "modify.txt").write_text("after\n")
        (self.paths.workspace / "delete.txt").unlink()
        (self.paths.workspace / "new.bin").write_bytes(b"\x00\xff")
        (self.paths.workspace / "mode.sh").chmod(0o755)

        first = build_changeset(self.paths)
        second = build_changeset(self.paths)
        self.assertEqual(first.change_set_hash, second.change_set_hash)
        self.assertEqual([(str(e.path), e.operation, e.kind) for e in first.entries], [
            ("delete.txt", "delete", "text"),
            ("mode.sh", "modify", "text"),
            ("modify.txt", "modify", "text"),
            ("new.bin", "create", "binary"),
        ])
        self.assertTrue(first.approvable)
        self.assertIn("-before", next(e.preview for e in first.entries if str(e.path) == "modify.txt"))

    def test_rejects_denylisted_new_file_and_new_or_changed_symlink(self):
        (self.source / "target").write_text("ok")
        (self.source / "link").symlink_to("target")
        self.snapshot()
        (self.paths.workspace / ".env").write_text("secret")
        (self.paths.workspace / "link").unlink()
        (self.paths.workspace / "link").symlink_to("other")
        changes = build_changeset(self.paths)
        self.assertFalse(changes.approvable)
        self.assertTrue(any("denylisted" in reason for reason in changes.violations))
        self.assertTrue(any("symlink" in reason for reason in changes.violations))

    def test_rejects_special_files_and_limits(self):
        self.snapshot()
        os.mkfifo(self.paths.workspace / "pipe")
        with self.assertRaisesRegex(ChangeSetError, "special"):
            build_changeset(self.paths)
        (self.paths.workspace / "pipe").unlink()
        (self.paths.workspace / "large").write_bytes(b"x" * 11)
        with self.assertRaisesRegex(ChangeSetError, "per-file"):
            build_changeset(self.paths, max_file_bytes=10)

    def test_total_limit_counts_only_changed_content(self):
        (self.source / "unchanged").write_bytes(b"x" * 20)
        (self.source / "changed").write_bytes(b"a")
        self.snapshot()
        (self.paths.workspace / "changed").write_bytes(b"bb")
        changes = build_changeset(self.paths, max_total_bytes=2)
        self.assertEqual([str(entry.path) for entry in changes.entries], ["changed"])

    def test_mutation_after_seal_invalidates_changeset(self):
        (self.source / "x.txt").write_text("before")
        self.snapshot()
        (self.paths.workspace / "x.txt").write_text("after")
        changes = build_changeset(self.paths)
        self.assertTrue(verify_sealed_changeset(self.paths, changes))
        (self.paths.workspace / "x.txt").write_text("mutated")
        self.assertFalse(verify_sealed_changeset(self.paths, changes))


if __name__ == "__main__":
    unittest.main()
