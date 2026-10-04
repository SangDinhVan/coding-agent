import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import ControlPaths
from sandbox.workspace import SnapshotError, prepare_workspace


class SnapshotAdmissionTests(unittest.TestCase):
    def test_selected_root_replacement_rejects_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'; source.mkdir()
            (source / 'a').write_text('selected')
            paths = ControlPaths.create(root / 'state', 's', source)
            source.rename(root / 'old-source')
            source.mkdir()
            (source / 'a').write_text('unselected')
            with self.assertRaisesRegex(SnapshotError, 'source_selection_changed'):
                prepare_workspace(source, paths, free_space_floor_bytes=0)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.state = self.root / 'state'

    def paths(self):
        return ControlPaths.create(self.state, 's', self.source)

    def snapshot(self, **kwargs):
        return prepare_workspace(self.source, self.paths(), free_space_floor_bytes=0, **kwargs)

    def test_overlap_has_no_side_effect(self):
        mode = self.source.stat().st_mode
        for root in (self.source, self.source / 'new-state', self.root):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, 'overlap'):
                ControlPaths.create(root, 's', self.source)
        self.assertEqual(self.source.stat().st_mode, mode)
        self.assertFalse((self.source / 'new-state').exists())
        self.assertFalse((self.root / 'sandboxes').exists())

    def test_inode_and_source_races(self):
        original = self.source / 'dirty.py'
        original.write_text('dirty bytes')
        original.chmod(0o640)
        self.snapshot()
        private = self.paths().workspace / original.name
        self.assertEqual(private.read_bytes(), original.read_bytes())
        self.assertNotEqual(private.stat().st_ino, original.stat().st_ino)
        self.assertEqual(private.stat().st_mode & 0o777, 0o640)
        os.link(original, self.source / 'hardlink')
        with self.assertRaisesRegex(SnapshotError, 'hardlink'):
            self.snapshot()
        (self.source / 'hardlink').unlink()
        (self.source / 'link').symlink_to('dirty.py')
        with self.assertRaisesRegex(SnapshotError, 'symlink'):
            self.snapshot()
        (self.source / 'link').unlink()
        os.mkfifo(self.source / 'fifo')
        with self.assertRaisesRegex(SnapshotError, 'unsupported'):
            self.snapshot()
        (self.source / 'fifo').unlink()
        original_read = os.read
        changed = [False]
        def change(fd, count):
            data = original_read(fd, count)
            if data and not changed[0]:
                changed[0] = True
                original.write_text('replacement')
            return data
        with patch('sandbox.generations.os.read', side_effect=change), self.assertRaisesRegex(SnapshotError, 'changed'):
            self.snapshot()

    def test_parent_replacement_cannot_admit_outside_bytes(self):
        (self.source / 'pkg').mkdir()
        (self.source / 'pkg' / 'a.py').write_text('safe')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'a.py').write_text('secret outside')
        original_open = os.open
        changed = [False]
        def swap(path, flags, *args, **kwargs):
            if path == 'pkg' and not changed[0]:
                changed[0] = True
                (self.source / 'pkg').rename(self.source / 'old-pkg')
                (self.source / 'pkg').symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)
        with patch('sandbox.generations.os.open', side_effect=swap), self.assertRaises(SnapshotError):
            self.snapshot()
        self.assertFalse(self.paths().baseline_manifest.exists())

    def test_bounded_admission(self):
        (self.source / 'a').write_text('x')
        (self.source / 'b').write_text('y')
        with self.assertRaisesRegex(SnapshotError, 'entries'):
            self.snapshot(max_entries=1)
        with self.assertRaisesRegex(SnapshotError, 'limit'):
            self.snapshot(baseline_limit_bytes=1)
