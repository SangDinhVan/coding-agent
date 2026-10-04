"""Physical acceptance of the supported rootless disposable RO profile."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from core.paths import ControlPaths
from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.models import ApprovalDecision
from sandbox.docker import DockerBackend, DockerIsolationLevel
from sandbox.generations import Generation, copy_tree, seal_generation
from sandbox.models import ExecRequest, ResourceLimits
from sandbox.session import SandboxSession
from tests.test_tool_lifecycle import call
from tools.filesystem import WriteTool
from tools.terminal import BashTool


@unittest.skipUnless(os.environ.get('RUN_SANDBOX_INTEGRATION') == '1', 'physical Docker acceptance is opt-in')
class DisposableROIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.image = os.environ['SANDBOX_IMAGE']
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'main.py').write_text('print(1)\n')
        self.limits = ResourceLimits(memory_bytes=256*1024**2, pids=64, tmpfs_bytes=1024**2, tmpfs_inodes=128, output_bytes=4096, tool_timeout_seconds=2)
        sealed = seal_generation(self.source, self.root / 'generations', max_bytes=100*1024**2, max_entries=10000)
        view = self.root / 'view'
        view.mkdir()
        copy_tree(sealed.directory, view, max_bytes=100*1024**2, max_entries=10000)
        view.chmod(0o755)
        self.generation = Generation(sealed.digest, view, sealed.manifest)
        self.backend = DockerBackend(self.image)
        self.backend.preflight(self.limits)
        self.assertEqual(self.backend.isolation_level, DockerIsolationLevel.ROOTLESS)
        self.addCleanup(self.backend.destroy)

    def execute(self, command, timeout=2):
        result = self.backend.exec_action(ExecRequest(uuid4().hex, command, timeout_seconds=timeout), self.generation, self.limits)
        self.assertTrue(result.quiescent)
        self.assertIsNone(self.backend.container_id)
        return result

    def test_success_descendant_and_timeout_revocation(self):
        observed_pids = []
        revoke = self.backend.revoke_action
        def inspect_then_revoke():
            cid = self.backend.container_id
            if cid:
                listing = self.backend._checked(['docker', 'top', cid, '-eo', 'pid'])
                observed_pids.extend(int(line.strip()) for line in listing.splitlines()[1:] if line.strip().isdigit())
            revoke()
        self.backend.revoke_action = inspect_then_revoke
        result = self.execute("python -c \"import os,time; p=os.fork(); (os.setsid(),os.close(0),os.close(1),os.close(2),time.sleep(60)) if p==0 else None\"")
        self.assertTrue(result.success)
        self.assertGreaterEqual(len(observed_pids), 2)
        self.assertTrue(all(not Path('/proc').joinpath(str(pid)).exists() for pid in observed_pids))
        timed = self.execute('sleep 60', timeout=0.2)
        self.assertEqual(timed.status.value, 'timed_out')
        self.assertEqual(timed.enforcement_observation, 'unknown')

    def test_cancel_revokes_container(self):
        helper = self.backend._helper
        def cancel(*args, **kwargs):
            raise KeyboardInterrupt()
        self.backend._helper = cancel
        with self.assertRaises(KeyboardInterrupt):
            self.execute('sleep 60')
        self.assertIsNone(self.backend.container_id)
        self.backend._helper = helper

    def test_ro_scope_and_clean_environment(self):
        result = self.execute("python - <<'CODE'\nimport os,pathlib\nassert not any(k in os.environ for k in ('API_KEY','DOCKER_HOST','BASE_URL'))\nassert not pathlib.Path('/var/run/docker.sock').exists()\nfor p in ('/workspace/main.py','/workspace/absent','/workspace/conftest.py','/etc/canary'):\n try: pathlib.Path(p).write_text('bad')\n except OSError: pass\n else: raise AssertionError(p)\nCODE")
        self.assertTrue(result.success, result.stderr)
        self.assertEqual((self.source / 'main.py').read_text(), 'print(1)\n')

    def test_network_external_denied_loopback_namespace_possible(self):
        result = self.execute("python - <<'CODE'\nimport socket\ns=socket.socket(); s.settimeout(.2)\ntry: s.connect(('169.254.169.254',80))\nexcept OSError: pass\nelse: raise AssertionError('metadata reachable')\ns.close()\ns=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); c=socket.socket(); c.connect(s.getsockname()); a,_=s.accept(); a.close(); c.close(); s.close()\nCODE")
        self.assertTrue(result.success, result.stderr)

    def test_tmpfs_byte_and_inode_exhaustion(self):
        for body in ("open('/tmp/huge','wb').write(b'x'*(2*1024**2))", "[open('/tmp/f'+str(i),'w').close() for i in range(200)]"):
            result = self.execute("python - <<'CODE'\ntry:\n " + body + "\nexcept OSError as e:\n assert e.errno==28\nelse: raise AssertionError('budget absent')\nCODE")
            self.assertTrue(result.success, result.stderr)

    def test_output_budget_and_nonzero(self):
        result = self.execute("python -c \"print('x'*10000)\"")
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode()), 4096)
        self.assertFalse(self.execute('exit 7').success)

    def test_private_edit_then_content_bound_ro_test(self):
        (self.source / '.env').write_text('SECRET=host-canary')
        original = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino) for p in self.source.iterdir()}
        paths = ControlPaths.create(self.root / 'state', uuid4().hex, self.source)
        backend = DockerBackend(self.image)
        self.addCleanup(backend.destroy)
        session = SandboxSession(paths.session_dir.name, self.source, paths, backend, self.limits, free_space_floor_bytes=0)
        session.create(); session.start()
        with EventStore(self.root / 'events.jsonl', session_id=session.session_id) as store:
            tools = {'write': WriteTool(session), 'bash': BashTool(session)}
            executor = ToolExecutor(store, get_tool=tools.get, policy=lambda execution: 'allow', get_security_context=session.security_context, approval_handler=lambda request: ApprovalDecision(True))
            for name,args in [('write',{'path':'main.py','content':'print(2)\n'}), ('bash',{'command':'python main.py'})]:
                identity = executor.request_batch([call(name=name,arguments=json.dumps(args))], 'turn')[0]
                result = executor.execute(identity)
                self.assertTrue(result.success, result.raw)
            self.assertIn('2', result.raw)
            identity = executor.request_batch([call(name='bash',arguments=json.dumps({'command':"python -c \"open('cache','w').write('x')\""}))], 'turn')[0]
            self.assertFalse(executor.execute(identity).success)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino) for p in self.source.iterdir()}, original)

    def test_end_to_end_modes_export_and_source_intact(self):
        from agent.loop import Agent
        from runtime.safety import AuthorityRecord, SafetyConfig
        from runtime.reducer import replay
        for mode in ('ask_all', 'ask_on_escalation', 'auto_review'):
            with self.subTest(mode=mode):
                identity = uuid4().hex
                paths = ControlPaths.create(self.root / ('state-' + mode), identity, self.source)
                backend = DockerBackend(self.image)
                self.addCleanup(backend.destroy)
                session = SandboxSession(identity, self.source, paths, backend, self.limits, free_space_floor_bytes=0)
                session.create(); session.start()
                destination = self.root / ('export-' + mode)
                destination.mkdir()
                approvals = []
                def approve(request):
                    approvals.append(request)
                    return ApprovalDecision(True)
                export_authority = AuthorityRecord('explicit-user-export', identity, session.authority.source_selection_digest,
                                                   str(destination), ('export_patch',), ('patch_export',))
                agent = Agent(str(self.root / (identity + '.jsonl')), workdir=str(self.source), sandbox=session,
                              state_root=paths.root, export_root=destination, export_authority=export_authority,
                              safety_config=SafetyConfig(approval_mode=mode), approval_handler=approve)
                self.addCleanup(agent.close)
                before = ((self.source / 'main.py').read_bytes(), (self.source / 'main.py').stat().st_ino)
                for name,args in [('write', {'path':'main.py','content':'print(3)\n'}),
                                  ('write', {'path':'conftest.py','content':'# approved control\n'}),
                                  ('bash', {'command':'python main.py'})]:
                    execution = agent.tool_executor.request_batch([call(name=name,arguments=json.dumps(args))], 't')[0]
                    result = agent.tool_executor.execute(execution)
                    self.assertTrue(result.success, result.raw)
                self.assertEqual(len(approvals), 3 if mode == 'ask_all' else 2)
                changes = agent.prepare_changes()
                exported = agent.export_changes('review.patch', changes.change_set_hash)
                self.assertIn('+print(3)', exported.read_text())
                self.assertEqual(((self.source / 'main.py').read_bytes(), (self.source / 'main.py').stat().st_ino), before)
                self.assertFalse((self.source / 'conftest.py').exists())
                self.assertTrue(replay(agent.event_store.read_all()).security_state.agent_modified_content)

    def test_physical_secret_stdout_blocks_disclosure_and_later_effects(self):
        from agent.loop import Agent
        from runtime.safety import DisclosureDenied
        from runtime.reducer import replay
        identity = uuid4().hex
        paths = ControlPaths.create(self.root / 'secret-state', identity, self.source)
        backend = DockerBackend(self.image)
        self.addCleanup(backend.destroy)
        session = SandboxSession(identity, self.source, paths, backend, self.limits, free_space_floor_bytes=0)
        session.create(); session.start()
        agent = Agent(str(self.root / (identity + '.jsonl')), workdir=str(self.source), sandbox=session,
                      state_root=paths.root, approval_handler=lambda request: ApprovalDecision(True))
        self.addCleanup(agent.close)
        command = "python -c \"print('SECRET=' + ''.join(map(chr,[99,97,110,97,114,121,45,118,97,108,117,101])))\""
        execution = agent.tool_executor.request_batch([call(name='bash',arguments=json.dumps({'command':command}))], 't')[0]
        result = agent.tool_executor.execute(execution)
        self.assertFalse(result.success)
        self.assertNotIn('SECRET=canary-value', agent.event_store.path.read_text())
        self.assertTrue(replay(agent.event_store.read_all()).security_state.secret_exposure_detected)
        with self.assertRaises(DisclosureDenied):
            agent.gate_text('benign', 'main_model')
        execution = agent.tool_executor.request_batch([call(name='write', arguments=json.dumps({'path':'later','content':'blocked'}))], 't')[0]
        self.assertFalse(agent.tool_executor.execute(execution).success)
        self.assertFalse((paths.workspace / 'later').exists())
