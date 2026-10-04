"""S2 physical acceptance; skips never certify the file-adapter profile."""
import os
import tempfile
import unittest
from pathlib import Path

from core.paths import ControlPaths
from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.models import ApprovalDecision
from sandbox.docker import DockerBackend, DockerIsolationLevel
from sandbox.models import ResourceLimits
from sandbox.session import SandboxSession
from tests.test_tool_lifecycle import call
from tools.filesystem import ReadTool, WriteTool


@unittest.skipUnless(os.environ.get('RUN_SANDBOX_INTEGRATION') == '1', 'set RUN_SANDBOX_INTEGRATION=1')
class RootlessFileAdapterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ.get('SANDBOX_IMAGE', '')
        if not cls.image:
            raise unittest.SkipTest('unmet S2 gate: pinned SANDBOX_IMAGE with current trusted helper is required')
        probe = DockerBackend(cls.image)
        probe.preflight(ResourceLimits())
        if probe.isolation_level != DockerIsolationLevel.ROOTLESS:
            raise RuntimeError('unmet S2 gate: rootless Docker is required')

    def test_granted_private_edit_and_unchanged_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            original = source / 'main.py'
            original.write_text('before\n')
            original.chmod(0o640)
            (source / '.env').write_text('PRIVATE_CANARY=not-admitted')
            paths = ControlPaths.create(root / 'state', 'it-file', source)
            backend = DockerBackend(self.image)
            session = SandboxSession('it-file', source, paths, backend, ResourceLimits(), free_space_floor_bytes=0)
            self.addCleanup(backend.destroy)
            before = (original.read_bytes(), original.stat().st_ino, original.stat().st_mode)
            session.create()
            session.start()
            self.assertTrue(session.security_context()['capabilities']['file_adapter'])
            with EventStore(root / 'events.jsonl', session_id='it-file') as store:
                tools = {'write': WriteTool(session), 'read': ReadTool(session)}
                executor = ToolExecutor(store, get_tool=tools.get, policy=lambda execution: 'allow',
                                        get_security_context=session.security_context,
                                        approval_handler=lambda request: ApprovalDecision(True))
                edit_id = executor.request_batch([call(name='write', arguments='{"path":"main.py","content":"after\\n"}')], 't')[0]
                self.assertTrue(executor.execute(edit_id).success)
                read_id = executor.request_batch([call(name='read', arguments='{"path":"main.py"}')], 't')[0]
                self.assertTrue(executor.execute(read_id).success)
                denied = executor.request_batch([call(name='write', arguments='{"path":".env","content":"forbidden"}')], 't')[0]
                self.assertFalse(executor.execute(denied).success)
            self.assertEqual((original.read_bytes(), original.stat().st_ino, original.stat().st_mode), before)
            self.assertEqual((paths.workspace / 'main.py').read_text(), 'after\n')
            self.assertFalse((paths.workspace / '.env').exists())

    def test_published_files_visible_in_project_in_all_approval_modes(self):
        import json
        from uuid import uuid4
        from runtime.safety import SafetyConfig
        from tools.filesystem import EditTool
        for mode in ('ask_all', 'ask_on_escalation', 'auto_review'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / 'source'; source.mkdir()
                (source / 'untouched.txt').write_text('user file')
                identity = uuid4().hex
                paths = ControlPaths.create(root / 'state', identity, source)
                backend = DockerBackend(self.image)
                self.addCleanup(backend.destroy)
                session = SandboxSession(identity, source, paths, backend, ResourceLimits(),
                                         free_space_floor_bytes=0, publish_files=True)
                session.create(); session.start()
                with EventStore(root / 'events.jsonl', session_id=identity) as store:
                    tools = {'write': WriteTool(session), 'edit': EditTool(session)}
                    decisions = []
                    def approve(request):
                        decisions.append(request)
                        return ApprovalDecision(True)
                    executor = ToolExecutor(store, get_tool=tools.get, policy=lambda execution:'allow',
                        get_security_context=session.security_context, safety_config=SafetyConfig(approval_mode=mode),
                        approval_handler=approve)
                    for name, args in [('write', {'path':'web/index.html','content':'<h1>Tea</h1>'}),
                                       ('edit', {'path':'web/index.html','old_string':'Tea','new_string':'Milk tea'})]:
                        identity = executor.request_batch([call(name=name, arguments=json.dumps(args))], 't')[0]
                        result = executor.execute(identity)
                        self.assertTrue(result.success, result.raw)
                        self.assertEqual(result.metadata['published_path'], str(source / 'web/index.html'))
                    self.assertEqual(len(decisions), 2 if mode=='ask_all' else 0)
                    self.assertEqual((source / 'web/index.html').read_text(), '<h1>Milk tea</h1>')
                    self.assertEqual((paths.workspace / 'web/index.html').read_text(), '<h1>Milk tea</h1>')
                    self.assertEqual((source / 'untouched.txt').read_text(), 'user file')

    def test_real_helper_rejects_changed_grant_and_parent(self):
        import json
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'; source.mkdir()
            (source / 'pkg').mkdir()
            (source / 'pkg' / 'a.py').write_text('before')
            identity = uuid4().hex
            paths = ControlPaths.create(root / 'state', identity, source)
            backend = DockerBackend(self.image)
            self.addCleanup(backend.destroy)
            session = SandboxSession(identity, source, paths, backend, ResourceLimits(), free_space_floor_bytes=0)
            session.create(); session.start()
            helper = session._trusted_file_helper()
            request = {'operation':'write','path':'pkg/a.py','content':'after'}
            grant = helper.make_grant(request, paths.workspace)
            changed = request | {'content':'different'}
            self.assertFalse(backend.fs_call(changed, grant=grant)['success'])
            limit = dict(grant, max_bytes=1)
            self.assertFalse(backend.fs_call(request, grant=limit)['success'])
            (paths.workspace / 'pkg').rename(paths.workspace / 'old-pkg')
            (paths.workspace / 'pkg').symlink_to(source / 'pkg')
            self.assertFalse(backend.fs_call(request, grant=grant)['success'])
            self.assertEqual((source / 'pkg' / 'a.py').read_text(), 'before')
