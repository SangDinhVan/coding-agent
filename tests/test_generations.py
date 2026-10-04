import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox.generations import seal_generation, verify_generation


class GenerationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.file = self.workspace / 'a.py'
        self.file.write_text('before')
        self.destination = self.root / 'generations'

    def seal(self, **kwargs):
        return seal_generation(self.workspace, self.destination, max_bytes=100, max_entries=10, **kwargs)

    def test_private_seal_and_binding(self):
        first = self.seal()
        second = self.seal()
        self.assertEqual(first.digest, second.digest)
        sealed = first.directory / 'a.py'
        self.assertNotEqual(sealed.stat().st_ino, self.file.stat().st_ino)
        self.file.write_text('after')
        self.assertEqual(sealed.read_text(), 'before')
        self.assertTrue(verify_generation(first))
        self.assertNotEqual(first.digest, self.seal().digest)
        old = self.seal()
        self.file.chmod(0o755)
        self.assertNotEqual(old.digest, self.seal().digest)
        self.assertFalse(first.directory.is_relative_to(self.workspace))
        sealed.chmod(0o600)
        sealed.write_text('tampered')
        self.assertFalse(verify_generation(first))

    def test_failed_or_changed_seal_never_current(self):
        (self.workspace / 'link').symlink_to('a.py')
        with self.assertRaises(RuntimeError):
            self.seal()
        (self.workspace / 'link').unlink()
        with patch('sandbox.generations.os.fsync', side_effect=OSError('disk full')), self.assertRaises(RuntimeError):
            self.seal()
        self.assertFalse(list(self.destination.glob('[0-9a-f]' * 64)))
        with self.assertRaises(RuntimeError):
            seal_generation(self.workspace, self.destination, max_bytes=1, max_entries=10)

    def test_nested_generation_verifies(self):
        (self.workspace / 'pkg').mkdir()
        (self.workspace / 'pkg' / 'b.py').write_text('nested')
        self.assertTrue(verify_generation(self.seal()))
