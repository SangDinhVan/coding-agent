import unittest
from pathlib import Path
from unittest.mock import Mock

from sandbox.docker import DockerBackend, DockerIsolationLevel, DockerPreflightError
from sandbox.models import ResourceLimits


class DockerFileAdapterTests(unittest.TestCase):
    def test_requires_rootless_and_pinned_helper(self):
        backend = DockerBackend('sha256:' + 'a' * 64)
        backend.container_id = 'cid'
        from sandbox.docker import IDLE_COMMAND
        backend.verify_runtime = Mock(return_value={'inspect': {'mounts': [{}], 'entrypoint':['/usr/bin/env'], 'command':IDLE_COMMAND}, 'probe': {}})
        backend.isolation_level = DockerIsolationLevel.VM_ISOLATED
        with self.assertRaises(DockerPreflightError):
            backend.verify_file_adapter(ResourceLimits(), Path('/workspace'), 'b' * 64)
        backend.isolation_level = DockerIsolationLevel.ROOTLESS
        backend._checked = Mock(return_value='c' * 64 + '\n')
        with self.assertRaises(DockerPreflightError):
            backend.verify_file_adapter(ResourceLimits(), Path('/workspace'), 'b' * 64)
        backend._checked = Mock(return_value='b' * 64 + '\n')
        backend.verify_helpers = lambda: None
        self.assertTrue(backend.verify_file_adapter(ResourceLimits(), Path('/workspace'), 'b' * 64)['file_adapter'])

    def test_helper_envelope_contains_separate_controller_grant(self):
        backend = DockerBackend('sha256:' + 'a' * 64)
        backend.container_id = 'cid'
        backend._helper = Mock(return_value={'success': True})
        request = {'operation': 'read', 'path': 'a.py'}
        grant = {'request_digest': 'b' * 64}
        backend.fs_call(request, grant=grant)
        self.assertEqual(backend._helper.call_args.args[1], {'request': request, 'grant': grant})
        with self.assertRaises(DockerPreflightError):
            backend.fs_call(request)
