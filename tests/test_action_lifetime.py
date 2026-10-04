import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock

from sandbox.docker import DockerBackend, DockerTransportError
from sandbox.generations import Generation
from sandbox.models import ExecRequest, ResourceLimits


class ActionLifetimeTests(unittest.TestCase):
    image = 'sha256:' + 'a' * 64

    def backend(self, failure=None):
        backend = DockerBackend(self.image)
        backend.create_action = Mock(return_value='action-cid')
        backend.start = Mock()
        backend.verify_action = Mock(return_value={})
        backend._helper = Mock(return_value={'status': 'success', 'stdout': 'ok', 'stderr': '', 'exit_code': 0, 'truncated': False})
        if failure:
            backend._helper.side_effect = failure
        backend.revoke_action = Mock()
        return backend

    def test_all_finally_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            generation = Generation('g', Path(folder), ())
            for failure in (None, ValueError('malformed'), DockerTransportError('lost'), KeyboardInterrupt()):
                with self.subTest(failure=type(failure).__name__):
                    backend = self.backend(failure)
                    if failure:
                        with self.assertRaises(BaseException):
                            backend.exec_action(ExecRequest('unique', 'true'), generation, ResourceLimits())
                    else:
                        result = backend.exec_action(ExecRequest('unique', 'true'), generation, ResourceLimits())
                        self.assertTrue(result.quiescent)
                        self.assertEqual(result.enforcement_observation, 'unknown')
                    backend.revoke_action.assert_called_once()

    def test_failed_cleanup_never_returns_success(self):
        backend = self.backend()
        backend.revoke_action.side_effect = DockerTransportError('unknown')
        with self.assertRaises(DockerTransportError):
            backend.exec_action(ExecRequest('a', 'true'), Generation('g', Path('/tmp'), ()), ResourceLimits())

    def test_lost_create_response_revokes_by_controller_token(self):
        from tests.test_docker_backend import FakeRunner, result
        identity = 'b' * 64
        runner = FakeRunner([subprocess.TimeoutExpired('docker create', 30),
                             result(identity), result(), result()])
        backend = DockerBackend(self.image, runner=runner)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(DockerTransportError):
                backend.exec_action(ExecRequest('a', 'true'), Generation('g', Path(folder), ()), ResourceLimits())
        self.assertEqual(len(runner.calls), 4)
        create = runner.calls[0][0]
        token = next(value for value in create if value.startswith('io.sang-coding-agent.creation='))
        self.assertEqual(runner.calls[1][0][-1], 'label=' + token)
        self.assertEqual(runner.calls[2][0], ['docker', 'rm', '--force', identity])
        self.assertEqual(runner.calls[3][0][-1], 'label=' + token)

    def test_lost_create_and_failed_lookup_still_denies(self):
        from tests.test_docker_backend import FakeRunner
        runner = FakeRunner([subprocess.TimeoutExpired('docker create', 30),
                             subprocess.TimeoutExpired('docker ls', 30)])
        backend = DockerBackend(self.image, runner=runner)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(DockerTransportError):
                backend.exec_action(ExecRequest('a', 'true'), Generation('g', Path(folder), ()), ResourceLimits())
        self.assertEqual(len(runner.calls), 2)

    def test_ro_profile_and_no_credentials(self):
        from tests.test_docker_backend import FakeRunner, result
        runner = FakeRunner([result('cid')])
        backend = DockerBackend(self.image, runner=runner)
        with tempfile.TemporaryDirectory() as folder:
            backend.create_action('a', Generation('g', Path(folder), ()), ResourceLimits(tmpfs_inodes=128))
        argv = runner.calls[0][0]
        self.assertIn('readonly', argv[argv.index('--mount') + 1])
        self.assertIn('nr_inodes=128', argv[argv.index('--tmpfs') + 1])
        self.assertEqual(argv[argv.index('--entrypoint') + 1], '/usr/bin/env')
        self.assertNotIn('--privileged', argv)
        self.assertIn('-i', argv)

    def test_file_adapter_also_has_fixed_idle_entrypoint(self):
        from tests.test_docker_backend import FakeRunner, result
        runner = FakeRunner([result('cid')])
        backend = DockerBackend(self.image, runner=runner)
        with tempfile.TemporaryDirectory() as folder:
            backend.create('file-session', 'scope', Path(folder), ResourceLimits())
        argv = runner.calls[0][0]
        self.assertIn('--entrypoint', argv)
        self.assertIn('-i', argv)
