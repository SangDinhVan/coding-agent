import tempfile
import unittest
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import ChangeSetError, apply_changeset, build_changeset, recover_incomplete_apply
from sandbox.workspace import prepare_workspace


class ChangeSetApplyTests(unittest.TestCase):
    def setUp(self):
        self.source_tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_tmp.cleanup)
        self.addCleanup(self.state_tmp.cleanup)
        self.source = Path(self.source_tmp.name)
        (self.source / "a.txt").write_text("a0")
        (self.source / "delete.txt").write_text("d0")
        self.paths = ControlPaths.create(self.state_tmp.name, "s1", self.source)
        prepare_workspace(self.source, self.paths, free_space_floor_bytes=0)
        (self.paths.workspace / "a.txt").write_text("a1")
        (self.paths.workspace / "delete.txt").unlink()
        (self.paths.workspace / "new.txt").write_text("new")
        self.changes = build_changeset(self.paths)

    def test_apply_requires_exact_hash_and_nonempty_note(self):
        with self.assertRaisesRegex(ChangeSetError, "hash"):
            apply_changeset(self.paths, self.source, self.changes, "wrong", "approved")
        with self.assertRaisesRegex(ChangeSetError, "note"):
            apply_changeset(self.paths, self.source, self.changes, self.changes.change_set_hash, "")

    def test_conflict_rejects_whole_set_without_mutation(self):
        (self.source / "a.txt").write_text("user edit")
        with self.assertRaisesRegex(ChangeSetError, "conflict"):
            apply_changeset(self.paths, self.source, self.changes, self.changes.change_set_hash, "approved")
        self.assertEqual((self.source / "a.txt").read_text(), "user edit")
        self.assertEqual((self.source / "delete.txt").read_text(), "d0")
        self.assertFalse((self.source / "new.txt").exists())

    def test_unrelated_host_edit_is_preserved(self):
        (self.source / "unrelated.txt").write_text("user")
        result = apply_changeset(self.paths, self.source, self.changes, self.changes.change_set_hash, "approved")
        self.assertTrue(result.success)
        self.assertEqual((self.source / "a.txt").read_text(), "a1")
        self.assertFalse((self.source / "delete.txt").exists())
        self.assertEqual((self.source / "new.txt").read_text(), "new")
        self.assertEqual((self.source / "unrelated.txt").read_text(), "user")

    def test_parent_symlink_is_rejected(self):
        (self.paths.workspace / "dir").mkdir()
        (self.paths.workspace / "dir/x").write_text("x")
        self.changes = build_changeset(self.paths)
        outside = self.source.parent / "apply-canary"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        (self.source / "dir").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ChangeSetError, "parent"):
            apply_changeset(self.paths, self.source, self.changes, self.changes.change_set_hash, "approved")
        self.assertFalse((outside / "x").exists())

    def test_apply_does_not_rename_across_state_and_workspace_filesystems(self):
        import errno
        import os
        from unittest.mock import patch

        real_replace = os.replace
        state_root = self.paths.root.resolve()
        source_root = self.source.resolve()

        def reject_cross_filesystem_replace(source, destination):
            source_path = Path(source).resolve()
            destination_path = Path(destination).resolve()
            crosses_boundary = (
                source_path.is_relative_to(state_root)
                and destination_path.is_relative_to(source_root)
            ) or (
                source_path.is_relative_to(source_root)
                and destination_path.is_relative_to(state_root)
            )
            if crosses_boundary:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(source, destination)

        with patch("sandbox.changes.os.replace", side_effect=reject_cross_filesystem_replace):
            result = apply_changeset(
                self.paths, self.source, self.changes,
                self.changes.change_set_hash, "approved",
            )

        self.assertTrue(result.success)
        self.assertEqual((self.source / "a.txt").read_text(), "a1")
        self.assertFalse((self.source / "delete.txt").exists())
        self.assertEqual((self.source / "new.txt").read_text(), "new")

    def test_injected_failure_rolls_back_all_prior_operations(self):
        with self.assertRaisesRegex(OSError, "injected"):
            apply_changeset(self.paths, self.source, self.changes, self.changes.change_set_hash, "approved", fail_after=2)
        self.assertEqual((self.source / "a.txt").read_text(), "a0")
        self.assertEqual((self.source / "delete.txt").read_text(), "d0")
        self.assertFalse((self.source / "new.txt").exists())
        self.assertFalse(recover_incomplete_apply(self.paths, self.source))


if __name__ == "__main__":
    unittest.main()
