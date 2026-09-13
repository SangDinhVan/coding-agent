import os
import tempfile
import unittest
from pathlib import Path

from core.paths import ControlPaths
from sandbox.workspace import SnapshotError, prepare_live_workspace, prepare_workspace


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.source_dir = tempfile.TemporaryDirectory()
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_dir.cleanup)
        self.addCleanup(self.state_dir.cleanup)
        self.source = Path(self.source_dir.name)
        self.paths = ControlPaths.create(self.state_dir.name, "session-1", self.source)

    def write(self, relative, content="data"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def snapshot(self):
        return prepare_workspace(self.source, self.paths, baseline_limit_bytes=1024 * 1024, free_space_floor_bytes=0)

    def test_hard_denylist_excludes_nested_and_fuzzy_secret_names(self):
        self.write("src/main.py", "print('ok')")
        self.write(".env", "API_KEY=never-copy")
        self.write("pkg/.ENV.production", "never-copy")
        self.write("nested/client_credentials_prod.json", "never-copy")
        manifest = self.snapshot()
        self.assertTrue((self.paths.workspace / "src/main.py").exists())
        self.assertFalse((self.paths.workspace / ".env").exists())
        self.assertFalse((self.paths.workspace / "pkg/.ENV.production").exists())
        self.assertFalse((self.paths.workspace / "nested/client_credentials_prod.json").exists())
        self.assertEqual({x.reason for x in manifest.excluded}, {"hard_security_denylist"})

    def test_agentignore_is_additive_and_negation_fails_closed(self):
        self.write("private/internal.cfg", "private")
        self.write(".agentignore", "private/\n")
        manifest = self.snapshot()
        excluded = {str(x.path): x.reason for x in manifest.excluded}
        self.assertEqual(excluded["private"], "project_agentignore")
        (self.source / ".agentignore").write_text("!.env\n", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotError, "negation"):
            self.snapshot()

    def test_gitignore_is_performance_exclusion(self):
        self.write("cache/value.bin", "large-ish")
        self.write("keep.py", "ok")
        self.write(".gitignore", "cache/\n")
        manifest = self.snapshot()
        self.assertFalse((self.paths.workspace / "cache").exists())
        self.assertIn(("cache", "performance_exclude"), {(str(x.path), x.reason) for x in manifest.excluded})

    def test_external_or_absolute_symlink_rejects_entire_snapshot(self):
        outside = self.write("outside.txt", "inside source")
        (self.source / "escape").symlink_to("../../etc/passwd")
        with self.assertRaisesRegex(SnapshotError, "symlink"):
            self.snapshot()
        (self.source / "escape").unlink()
        (self.source / "escape").symlink_to(outside.resolve())
        with self.assertRaisesRegex(SnapshotError, "symlink"):
            self.snapshot()

    def test_safe_relative_symlink_is_preserved_without_dereference(self):
        self.write("pkg/target.txt", "target")
        (self.source / "pkg/link.txt").symlink_to("target.txt")
        self.snapshot()
        copied = self.paths.workspace / "pkg/link.txt"
        self.assertTrue(copied.is_symlink())
        self.assertEqual(os.readlink(copied), "target.txt")

    def test_fifo_rejects_snapshot(self):
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(SnapshotError, "unsupported"):
            self.snapshot()

    def test_regular_files_are_independent_hashed_and_modes_sanitized(self):
        original = self.write("script.py", "print(1)")
        original.chmod(0o4755)
        manifest = self.snapshot()
        copied = self.paths.workspace / "script.py"
        self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
        self.assertEqual(copied.stat().st_mode & 0o7777, 0o777)
        self.assertEqual(self.paths.workspace.stat().st_mode & 0o7777, 0o777)
        entry = next(x for x in manifest.included if str(x.path) == "script.py")
        self.assertEqual(entry.mode, 0o755)
        self.assertEqual(len(entry.sha256), 64)
        copied.write_text("changed", encoding="utf-8")
        self.assertEqual(original.read_text(encoding="utf-8"), "print(1)")
        self.assertTrue(self.paths.shadow_git.is_dir())
        self.assertFalse((self.paths.workspace / ".git").exists())

    def test_shadow_git_baseline_is_clean_and_outside_mount(self):
        self.write("main.py", "print('baseline')")
        self.snapshot()
        import subprocess
        result = subprocess.run(
            ["git", f"--git-dir={self.paths.shadow_git}", f"--work-tree={self.paths.workspace}", "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.paths.shadow_git.is_relative_to(self.paths.workspace))

    def test_oversize_baseline_fails_before_copy(self):
        self.write("large.bin", "x" * 32)
        with self.assertRaisesRegex(SnapshotError, "baseline"):
            prepare_workspace(self.source, self.paths, baseline_limit_bytes=16, free_space_floor_bytes=0)


class LiveWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.source_dir = tempfile.TemporaryDirectory()
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_dir.cleanup)
        self.addCleanup(self.state_dir.cleanup)
        self.source = Path(self.source_dir.name)
        self.paths = ControlPaths.create(self.state_dir.name, "session-live", self.source)

    def write(self, relative, content="data"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
    def test_live_masks_security_agentignore_and_caches_but_not_gitignored_paths(self):
        keep = self.write("src/main.py", "print('ok')")
        secret = self.write(".env", "API_KEY=hidden")
        self.write("nested/client_credentials_prod.json", "hidden")
        generated = self.write("output/index.html", "generated")
        self.write("build/value.bin", "cache")
        self.write("private/internal.cfg", "private")
        self.write(".gitignore", "output/\n")
        self.write(".agentignore", "private/\n")
        self.write(".git/config", "private")
        before = {
            path.relative_to(self.source).as_posix(): (path.read_bytes(), path.stat().st_mode)
            for path in self.source.rglob("*") if path.is_file()
        }

        manifest = prepare_live_workspace(self.source, self.paths, free_space_floor_bytes=0)

        excluded = {str(entry.path): entry for entry in manifest.excluded}
        included = {str(entry.path): entry for entry in manifest.included}
        self.assertEqual(excluded[".git"].kind, "directory")
        self.assertEqual(excluded[".env"].kind, "file")
        self.assertIn("nested/client_credentials_prod.json", excluded)
        self.assertIn("private", excluded)
        self.assertIn("build", excluded)
        self.assertIn("output", included)
        self.assertIn("output/index.html", included)
        self.assertTrue(self.paths.mask_directory.is_dir())
        self.assertTrue(self.paths.mask_file.is_file())
        self.assertEqual(self.paths.mask_file.read_bytes(), b"")
        self.assertEqual(keep.read_text(encoding="utf-8"), "print('ok')")
        self.assertEqual(secret.read_text(encoding="utf-8"), "API_KEY=hidden")
        self.assertEqual(generated.read_text(encoding="utf-8"), "generated")
        after = {
            path.relative_to(self.source).as_posix(): (path.read_bytes(), path.stat().st_mode)
            for path in self.source.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertTrue(self.paths.baseline_manifest.is_file())

    def test_live_workspace_rejects_special_inode(self):
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(SnapshotError, "unsupported"):
            prepare_live_workspace(self.source, self.paths, free_space_floor_bytes=0)


if __name__ == "__main__":
    unittest.main()
