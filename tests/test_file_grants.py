import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_sandbox_helpers import load


class FileGrantTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.fs = load('sandbox_fs')

    def grant(self, request, **limits):
        return self.fs.make_grant(request, self.workspace, max_bytes=20, max_total_bytes=100, max_entries=10, **limits)

    def test_exact_request_and_control_diff(self):
        request = {'operation': 'write', 'path': 'Makefile', 'content': 'build'}
        self.assertFalse(self.fs.handle(request, self.workspace)['success'])
        grant = self.grant(request, protected=True)
        self.assertTrue(grant['protected_diff_digest'])
        for changed in (request | {'path': 'other'}, request | {'content': 'malicious'}, request | {'operation': 'read'}):
            self.assertFalse(self.fs.handle(changed, self.workspace, grant=grant)['success'])
        self.assertTrue(self.fs.handle(request, self.workspace, grant=grant)['success'])
        self.assertFalse(self.fs.handle(request, self.workspace, grant=grant)['success'])
        read = {'operation': 'read', 'path': 'Makefile'}
        self.assertEqual(self.fs.handle(read, self.workspace, grant=self.grant(read))['content'], 'build')

    def test_absent_target_and_parent_replacement(self):
        outside = self.workspace / 'outside'
        outside.mkdir()
        (outside / 'canary').write_text('original')
        request = {'operation': 'write', 'path': 'pkg/canary', 'content': 'new'}
        (self.workspace / 'pkg').mkdir()
        grant = self.grant(request)
        (self.workspace / 'pkg').rmdir()
        (self.workspace / 'pkg').symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.fs.handle(request, self.workspace, grant=grant)['success'])
        self.assertEqual((outside / 'canary').read_text(), 'original')
        (self.workspace / 'pkg').unlink()
        self.assertTrue(self.fs.handle(request, self.workspace, grant=self.grant(request))['success'])
        grant = self.grant(request)
        (self.workspace / 'pkg/canary').chmod(0o755)
        self.assertFalse(self.fs.handle(request, self.workspace, grant=grant)['success'])

    def test_bounded_atomic_write(self):
        oversized = {'operation': 'write', 'path': 'a', 'content': 'x' * 21}
        with self.assertRaises(ValueError):
            self.grant(oversized)
        request = {'operation': 'write', 'path': 'a', 'content': '123456789'}
        original_write = os.write
        def partial(fd, data):
            return original_write(fd, data[:2])
        with patch.object(self.fs.os, 'write', side_effect=partial):
            self.assertTrue(self.fs.handle(request, self.workspace, grant=self.grant(request))['success'])
        self.assertEqual((self.workspace / 'a').read_text(), '123456789')
        failure = request | {'content': 'other'}
        grant = self.grant(failure)
        with patch.object(self.fs.os, 'write', return_value=0):
            self.assertFalse(self.fs.handle(failure, self.workspace, grant=grant)['success'])
        self.assertEqual((self.workspace / 'a').read_text(), '123456789')
        with patch.object(self.fs.os, 'fsync', side_effect=OSError('disk full')):
            self.assertFalse(self.fs.handle(failure, self.workspace, grant=grant)['success'])
        self.assertEqual((self.workspace / 'a').read_text(), '123456789')

    def test_aggregate_entries_and_bytes(self):
        for index in range(10):
            (self.workspace / str(index)).write_text('x' * 10)
        request = {'operation': 'write', 'path': 'extra', 'content': 'x'}
        with self.assertRaises(ValueError):
            self.grant(request)
