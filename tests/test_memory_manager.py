import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.manager import MemoryManager


class MemoryManagerTests(unittest.TestCase):
    def manager(self, directory: str, name: str = "PROJECT.md") -> MemoryManager:
        return MemoryManager(Path(directory) / name)

    def test_remember_normalizes_and_deduplicates_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            entry, created = manager.remember("  Repository   uses pytest. ")
            self.assertTrue(created)
            self.assertEqual(entry.fact, "Repository uses pytest.")
            self.assertRegex(entry.memory_id, r"^[0-9a-f]{8}$")

            same, created = manager.remember("repository uses pytest.")
            self.assertFalse(created)
            self.assertEqual(same.memory_id, entry.memory_id)

    def test_forget_accepts_id_or_exact_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            first, _ = manager.remember("Repository uses pytest.")
            second, _ = manager.remember("Public API stays stable.")
            self.assertEqual(manager.forget(first.memory_id), first)
            self.assertEqual(manager.forget("public api stays stable."), second)
            self.assertEqual(manager.entries(), ())

    def test_workspaces_are_isolated_under_one_state_root(self):
        with tempfile.TemporaryDirectory() as root:
            first = MemoryManager(Path(root) / "projects" / "first" / "PROJECT.md")
            second = MemoryManager(Path(root) / "projects" / "second" / "PROJECT.md")
            first.remember("Only first workspace knows this.")
            self.assertIn("Only first workspace knows this.", first.read())
            self.assertNotIn("Only first workspace knows this.", second.read())

    def test_write_preserves_unmanaged_sections_and_private_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "PROJECT.md"
            manager = MemoryManager(path)
            original = manager.read().replace("(chưa có dữ liệu)", "Custom overview", 1)
            path.write_text(original, encoding="utf-8")
            manager.remember("Repository uses pytest.")
            self.assertIn("Custom overview", manager.read())
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_or_ambiguous_forget_does_not_rewrite_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            with patch("memory.manager.uuid4") as make_uuid:
                make_uuid.side_effect = [
                    unittest.mock.Mock(hex="deadbeef000000000000000000000000"),
                    unittest.mock.Mock(hex="cafebabe000000000000000000000000"),
                ]
                manager.remember("First fact")
                manager.remember("deadbeef")
            before = manager.project_md_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                manager.forget("deadbeef")
            self.assertEqual(manager.project_md_path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "not found"):
                manager.forget("missing")
            self.assertEqual(manager.project_md_path.read_bytes(), before)

    def test_empty_memory_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            with self.assertRaisesRegex(ValueError, "empty"):
                manager.remember("  \n ")
            with self.assertRaisesRegex(ValueError, "empty"):
                manager.forget(" ")

    def test_atomic_replace_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.remember("Existing fact")
            before = manager.project_md_path.read_bytes()
            with patch("memory.manager.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    manager.remember("New fact")
            self.assertEqual(manager.project_md_path.read_bytes(), before)

    def test_concurrent_mutations_do_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.manager(directory)
            second = self.manager(directory)
            first_read = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            second_done = threading.Event()
            errors = []

            first_entries = first.entries
            second_entries = second.entries

            def pause_after_first_read():
                entries = first_entries()
                first_read.set()
                release_first.wait(2)
                return entries

            def mark_second_read():
                second_entered.set()
                return second_entries()

            first.entries = pause_after_first_read
            second.entries = mark_second_read

            def remember(manager, fact, done=None):
                try:
                    manager.remember(fact)
                except BaseException as error:
                    errors.append(error)
                finally:
                    if done is not None:
                        done.set()

            first_thread = threading.Thread(target=remember, args=(first, "First fact"))
            second_thread = threading.Thread(
                target=remember, args=(second, "Second fact", second_done)
            )
            first_thread.start()
            self.assertTrue(first_read.wait(2))
            second_thread.start()
            if second_entered.wait(0.5):
                self.assertTrue(second_done.wait(2))
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                {entry.fact for entry in MemoryManager(first.project_md_path).entries()},
                {"First fact", "Second fact"},
            )


if __name__ == "__main__":
    unittest.main()
