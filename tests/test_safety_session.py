import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.paths import ControlPaths
from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.reducer import replay
from runtime.models import ToolExecutionStatus
from sandbox.models import ResourceLimits, WorkspaceMode
from sandbox.session import SandboxSession, SessionError
from tests.test_sandbox_session import FakeBackend
from tests.test_sandbox_helpers import load
from tests.test_tool_lifecycle import call
from tools.filesystem import WriteTool


class GrantedBackend(FakeBackend):
    image = 'sha256:' + 'a' * 64

    def verify_file_adapter(self, limits, workspace, helper_digest):
        return {'file_adapter': True, 'helper_digest': helper_digest, 'rootless': True}

    def fs_call(self, request, *, grant):
        return load('sandbox_fs').handle(request, self.workspace, grant=grant)


class ShellBackend(GrantedBackend):
    def __init__(self, image=None, *, runner=None, control_container_id=''):
        super().__init__()
        self.runner = runner
        self.control_container_id = control_container_id

    def verify_file_adapter(self, *args):
        return super().verify_file_adapter(*args) | {'opaque_ro': True}

    def exec_action(self, request, generation, limits):
        from sandbox.models import SandboxResult, ExecutionStatus, ViolationStatus
        data = load('sandbox_exec').execute({'command': request.command, 'cwd': str(request.cwd),
                                            'timeout_seconds': request.timeout_seconds}, generation.directory)
        return SandboxResult(ExecutionStatus(data['status']), data['stdout'], data['stderr'],
                             data['exit_code'], ViolationStatus.UNKNOWN, request.action_id,
                             'test-action', self.image, quiescent=True)


class SafetySessionTests(unittest.TestCase):
    def test_static_filter_cannot_extract_secret_values_without_their_label(self):
        from tools.terminal import BashTool

        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        self.backend = ShellBackend()
        self.session.backend = self.backend
        self.session.create(); self.session.start()
        with EventStore(self.root / 'filter.jsonl', session_id='s') as store:
            executor = ToolExecutor(store, get_tool=lambda name: BashTool(self.session),
                policy=lambda execution: 'allow', get_security_context=self.session.security_context)
            identity = executor.request_batch([call(name='bash', arguments='{"command":"grep -o canary-value fixture.txt"}')], 't')[0]
            result = executor.execute(identity)
            self.assertFalse(result.success)
            self.assertEqual(result.metadata['reason_code'], 'static_inputs_not_disclosable')
            self.assertNotIn('canary-value', result.raw)
            self.assertFalse(any(event['event_type'] == 'ToolStarted' for event in store.read_all()))

    def test_static_shell_works_with_unrelated_secret_and_output_disclosure_still_blocks(self):
        import json
        from runtime.models import ApprovalDecision
        from runtime.safety import SafetyConfig
        from tools.terminal import BashTool

        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        (self.source / 'index.html').write_text('<h1>Tea</h1>')
        (self.source / 'unrelated.png').write_bytes(b'\xff\x00')
        self.backend = ShellBackend()
        self.session.backend = self.backend
        self.session.create(); self.session.start()
        for mode in ('ask_all', 'ask_on_escalation', 'auto_review'):
            with self.subTest(mode=mode), EventStore(self.root / (mode + '.jsonl'), session_id='s') as store:
                previews = []

                def approve(request):
                    previews.append(self.session.approval_preview(replay(store.read_all()).executions[request.execution_id]))
                    return ApprovalDecision(True)

                executor = ToolExecutor(store, get_tool=lambda name: BashTool(self.session),
                    policy=lambda execution: 'allow', get_security_context=self.session.security_context,
                    safety_config=SafetyConfig(approval_mode=mode), approval_handler=approve)
                for command in ('ls -la', 'find . -maxdepth 1 -type f', 'cat index.html'):
                    identity = executor.request_batch([call(name='bash', arguments=json.dumps({'command': command}))], 't')[0]
                    result = executor.execute(identity)
                    self.assertTrue(result.success, result.raw)
                self.assertIn('<h1>Tea</h1>', result.raw)
                self.assertFalse(replay(store.read_all()).security_state.secret_exposure_detected)
                self.assertTrue(all('shell_facts' in preview and 'sealed_inputs' not in preview for preview in previews))
                self.assertEqual(len(previews), 3 if mode == 'ask_all' else 0)
                identity = executor.request_batch([call(name='bash', arguments='{"command":"cat fixture.txt"}')], 't')[0]
                self.assertEqual(executor.execute(identity).metadata['reason_code'], 'static_inputs_not_disclosable')
                self.assertNotIn('canary-value', store.path.read_text())

    def test_html_creation_and_read_verification_use_file_evidence_without_shell(self):
        import json
        from agent.loop import Agent
        from tests.fakes import stream_text, stream_tool_call

        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        self.session.publish_files = True
        self.session.create(); self.session.start()
        agent = Agent(str(self.root / 's.jsonl'), workdir=str(self.source), sandbox=self.session)
        self.addCleanup(agent.close)
        responses = [
            stream_tool_call('plan', 'update_plan', json.dumps({'action': 'create_plan', 'steps': [
                {'task': 'Create index.html', 'completion_policy': 'evidence_required'},
                {'task': 'Verify saved HTML contents', 'completion_policy': 'evidence_required'},
            ]})),
            stream_tool_call('write', 'write', '{"path":"index.html","content":"<h1>Tea</h1>"}'),
            stream_tool_call('created', 'update_plan', '{"action":"complete_step","step_id":"step_1","evidence_execution_ids":["exec_2"]}'),
            stream_tool_call('read', 'read', '{"path":"index.html"}'),
            stream_tool_call('verified', 'update_plan', '{"action":"complete_step","step_id":"step_2","evidence_execution_ids":["exec_4"]}'),
            stream_text('created and inspected'),
        ]
        with patch('agent.loop.llm.complete', side_effect=responses):
            self.assertEqual(agent.run_turn('create and inspect html'), 'created and inspected')
        self.assertEqual((self.source / 'index.html').read_text(), '<h1>Tea</h1>')
        steps = agent.runtime_state.plans[agent.turn_state.plan_id].revisions[-1].steps
        self.assertTrue(all(step.status.value == 'completed' for step in steps))
        self.assertEqual([agent.runtime_state.executions[step.evidence_execution_ids[0]].tool_name
                          for step in steps], ['write', 'read'])
        self.assertFalse(any(item.tool_name == 'bash' for item in agent.runtime_state.executions.values()))

    def test_planned_html_creation_does_not_scan_workspace_during_plan_approval(self):
        import json
        from agent.loop import Agent
        from main import run_repl
        from runtime.safety import SafetyConfig
        from tests.fakes import stream_text, stream_tool_call
        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        self.session.publish_files = True
        verifier = self.backend.verify_file_adapter
        with patch.object(self.backend, 'verify_file_adapter',
                          side_effect=lambda *args: verifier(*args) | {'opaque_ro':True}):
            self.session.create(); self.session.start()
        events = self.root / 's.jsonl'
        agent = Agent(str(events), workdir=str(self.source), sandbox=self.session,
                      safety_config=SafetyConfig(approval_mode='ask_all'))
        responses = [
            stream_tool_call('inspect', 'bash', json.dumps({'command':"ls -la && find . -maxdepth 2 -type f | sed 's#^./##' | head -100",'cwd':'.','verification_kind':'structural'})),
            stream_tool_call('code', 'bash', '{"command":"python main.py","cwd":".","verification_kind":"structural"}'),
            stream_tool_call('plan', 'update_plan', '{"action":"create_plan","steps":[{"task":"Create milk tea HTML","completion_policy":"self_attested"}]}'),
            stream_tool_call('write', 'write', '{"path":"index.html","content":"<h1>Milk tea</h1>"}'),
            stream_tool_call('done', 'update_plan', '{"action":"complete_step","step_id":"step_1","note":"HTML saved to project"}'),
            stream_text('Created index.html in your project'),
        ]
        inputs = iter(['create a milk tea website', '1', '1', '1', 'exit'])
        prompts, output = [], []
        def read_input(prompt):
            prompts.append(prompt)
            try:
                return next(inputs)
            except StopIteration:
                self.fail('The plan and file approval flow unexpectedly asks for more input')
        with patch('agent.loop.llm.complete', side_effect=responses) as model:
            run_repl(events, agent_factory=lambda path:agent, input_fn=read_input, print_fn=output.append)
        self.assertTrue((self.source / 'index.html').is_file(), output)
        self.assertEqual((self.source / 'index.html').read_text(), '<h1>Milk tea</h1>')
        self.assertFalse(replay(agent.event_store.read_all()).security_state.secret_exposure_detected)
        self.assertEqual(sum('Approval decision' in prompt for prompt in prompts), 3)
        self.assertIsNone(agent.runtime_state.active_turn_id)
        self.assertNotIn('canary-value', '\n'.join(output))
        self.assertFalse(any('sealed_inputs' in item for item in output))
        self.assertNotIn('canary-value', str([call.kwargs['messages'] for call in model.call_args_list]))

    def publishing_executor(self, approval_handler=None):
        from runtime.safety import SafetyConfig
        from tools.filesystem import EditTool
        self.session.publish_files = True
        self.session.create(); self.session.start()
        store = EventStore(self.root / 'publishing.jsonl', session_id='s')
        self.addCleanup(store.close)
        tools = {'write': WriteTool(self.session), 'edit': EditTool(self.session)}
        return ToolExecutor(store, get_tool=tools.get, policy=lambda execution: 'allow',
            safety_config=SafetyConfig(approval_mode='ask_all'), approval_handler=approval_handler,
            get_security_context=self.session.security_context), store

    def test_approved_files_appear_in_selected_project_and_rejection_does_not(self):
        import json
        executor, store = self.publishing_executor()
        first = executor.request_batch([call(name='write', arguments='{"path":"web/index.html","content":"<h1>Tea</h1>"}')], 't')[0]
        executor.execute(first)
        self.assertFalse((self.source / 'web').exists())
        self.assertTrue(executor.resolve_approval(first, True, '').success)
        self.assertEqual((self.source / 'web/index.html').read_text(), '<h1>Tea</h1>')
        args = json.dumps({'path':'web/index.html','old_string':'Tea','new_string':'Milk tea'})
        second = executor.request_batch([call(name='edit', arguments=args)], 't')[0]
        executor.execute(second)
        self.assertTrue(executor.resolve_approval(second, True, '').success)
        self.assertEqual((self.source / 'web/index.html').read_text(), '<h1>Milk tea</h1>')
        self.assertEqual((self.paths.workspace / 'web/index.html').read_text(), '<h1>Milk tea</h1>')
        denied = executor.request_batch([call(name='write', arguments='{"path":"rejected.html","content":"no"}')], 't')[0]
        executor.execute(denied)
        executor.resolve_approval(denied, False, '')
        self.assertFalse((self.source / 'rejected.html').exists())
        self.assertEqual((self.source / 'main.py').read_text(), 'before')

    def test_project_change_before_approval_prevents_both_writes(self):
        executor, store = self.publishing_executor()
        identity = executor.request_batch([call(name='write', arguments='{"path":"main.py","content":"agent"}')], 't')[0]
        executor.execute(identity)
        (self.source / 'main.py').write_text('user changes')
        self.assertFalse(executor.resolve_approval(identity, True, '').success)
        self.assertEqual((self.source / 'main.py').read_text(), 'user changes')
        self.assertEqual((self.paths.workspace / 'main.py').read_text(), 'before')
        self.assertFalse(any(event['event_type']=='ToolStarted' for event in store.read_all()))

    def test_publishing_denies_symlink_parents_and_protected_paths(self):
        executor, store = self.publishing_executor()
        outside = self.root / 'outside'; outside.mkdir()
        (self.source / 'escape').symlink_to(outside, target_is_directory=True)
        for path in ('escape/index.html', '.env', '.git/config'):
            import json
            identity = executor.request_batch([call(name='write', arguments=json.dumps({'path':path,'content':'no'}))], 't')[0]
            executor.execute(identity)
            if replay(store.read_all()).executions[identity].status==ToolExecutionStatus.WAITING_APPROVAL:
                self.assertFalse(executor.resolve_approval(identity, True, '').success)
        self.assertFalse((outside / 'index.html').exists())
        self.assertFalse((self.source / '.env').exists())
        self.assertFalse((self.source / '.git').exists())

    def test_publication_failure_requires_recovery_without_false_success(self):
        executor, store = self.publishing_executor()
        identity = executor.request_batch([call(name='write', arguments='{"path":"index.html","content":"html"}')], 't')[0]
        executor.execute(identity)
        with patch('sandbox.publication.publish_file', side_effect=OSError('disk full')):
            result = executor.resolve_approval(identity, True, '')
        self.assertFalse(result.success)
        self.assertFalse((self.source / 'index.html').exists())
        self.assertTrue(self.session.quarantined)
        self.assertEqual(replay(store.read_all()).executions[identity].status, ToolExecutionStatus.RECOVERY_REQUIRED)

    def test_adapter_failure_does_not_publish_and_requires_recovery(self):
        executor, store = self.publishing_executor()
        identity = executor.request_batch([call(name='write', arguments='{"path":"index.html","content":"html"}')], 't')[0]
        executor.execute(identity)
        with patch.object(self.backend, 'fs_call', return_value={'success':False,'status':'runtime_error','error':'disk full'}):
            self.assertFalse(executor.resolve_approval(identity, True, '').success)
        self.assertFalse((self.source / 'index.html').exists())
        self.assertEqual(replay(store.read_all()).executions[identity].status, ToolExecutionStatus.RECOVERY_REQUIRED)

    def test_publication_scope_survives_restart_and_legacy_stays_private(self):
        executor, store = self.publishing_executor()
        restored = SandboxSession('s', self.source, self.paths, self.backend, ResourceLimits(), free_space_floor_bytes=0)
        self.assertTrue(restored.publish_files)
        import json
        metadata = json.loads(self.paths.metadata.read_text())
        metadata.pop('publish_files')
        self.paths.metadata.write_text(json.dumps(metadata))
        legacy = SandboxSession('s', self.source, self.paths, self.backend, ResourceLimits(),
                                free_space_floor_bytes=0, publish_files=True)
        self.assertFalse(legacy.publish_files)

    def test_admission_exclusions_cannot_be_recreated(self):
        (self.source / 'private').mkdir()
        (self.source / 'private' / 'hidden.txt').write_text('excluded')
        (self.source / '.agentignore').write_text('private/\n')
        self.session.create(); self.session.start()
        self.assertEqual(self.session.security_context()['classify_path']('private/new.py'), 'excluded')

    def test_new_restrictions_remove_paths_from_opaque_view(self):
        self.session.create(); self.session.start()
        self.session.restrict_paths(('main.py',))
        from sandbox.generations import copy_tree
        view = self.root / 'view'; view.mkdir()
        entries, _, _ = copy_tree(self.paths.workspace, view, max_bytes=10000, max_entries=100,
                                  exclude=self.session._excluded_reason)
        self.assertFalse((view / 'main.py').exists())
        self.assertEqual(self.session.security_context()['classify_path']('main.py'), 'excluded')

    def test_recovery_rejects_same_bytes_with_changed_mode(self):
        from runtime.models import RecoveryDecision
        self.session.create(); self.session.start()
        store = EventStore(self.root / 'events.jsonl', session_id='s')
        self.addCleanup(store.close)
        tool = WriteTool(self.session)
        executor = ToolExecutor(store, get_tool=lambda name: tool, policy=lambda execution: 'allow', get_security_context=self.session.security_context)
        identity = executor.request_batch([call(name='write',arguments='{"path":"main.py","content":"after"}')], 't')[0]
        append = store.append_event
        def fail(kind, *args, **kwargs):
            if kind == 'ToolCompleted':
                raise OSError('audit lost')
            return append(kind, *args, **kwargs)
        with patch.object(store, 'append_event', side_effect=fail), self.assertRaises(OSError):
            executor.execute(identity)
        (self.paths.workspace / 'main.py').chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'state'):
            executor.resolve_recovery(identity, RecoveryDecision.COMPLETED, 'user reconcile')

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'main.py').write_text('before')
        self.paths = ControlPaths.create(self.root / 'state', 's', self.source)
        self.backend = GrantedBackend()
        self.session = SandboxSession('s', self.source, self.paths, self.backend, ResourceLimits(), free_space_floor_bytes=0)

    def test_shadow_default_and_live_resume_denied(self):
        self.assertEqual(self.session.mode, WorkspaceMode.SHADOW)
        with self.assertRaisesRegex(SessionError, 'unsupported_profile'):
            SandboxSession('s', self.source, self.paths, self.backend, ResourceLimits(), mode=WorkspaceMode.LIVE)

    def test_checkpoint_fsync_failure_blocks_tool(self):
        self.session.create()
        self.session.start()
        store = EventStore(self.root / 'events.jsonl', session_id='s')
        self.addCleanup(store.close)
        tool = WriteTool(self.session)
        executor = ToolExecutor(store, get_tool=lambda name: tool, policy=lambda execution: 'allow',
                                get_security_context=self.session.security_context)
        execution_id = executor.request_batch([call(name='write', arguments='{"path":"new/a.py","content":"after"}')], 't')[0]
        with patch.object(self.session, 'checkpoint_action', side_effect=OSError('disk full')):
            self.assertFalse(executor.execute(execution_id).success)
        self.assertEqual((self.source / 'main.py').read_text(), 'before')
        self.assertFalse((self.paths.workspace / 'new').exists())
        self.assertFalse(any(event['event_type'] in {'ToolStarted', 'ToolGrantClaimed'} for event in store.read_all()))

    def test_file_grant_and_checkpoint_integrate(self):
        self.session.create()
        self.session.start()
        store = EventStore(self.root / 'events.jsonl', session_id='s')
        self.addCleanup(store.close)
        tool = WriteTool(self.session)
        executor = ToolExecutor(store, get_tool=lambda name: tool, policy=lambda execution: 'allow',
                                get_security_context=self.session.security_context)
        original = self.session.security_context()['generation_digest']
        execution_id = executor.request_batch([call(name='write', arguments='{"path":"main.py","content":"after"}')], 't')[0]
        self.assertTrue(executor.execute(execution_id).success)
        self.assertEqual((self.source / 'main.py').read_text(), 'before')
        self.assertEqual((self.paths.workspace / 'main.py').read_text(), 'after')
        self.assertNotEqual(original, self.session.security_context()['generation_digest'])
        self.assertTrue(list(self.paths.checkpoints.glob('*')))
        self.assertEqual(replay(store.read_all()).executions[execution_id].status, ToolExecutionStatus.COMPLETED)
        with self.assertRaisesRegex(SessionError, 'grant'):
            self.session.fs_call({'operation': 'write', 'path': 'other', 'content': 'x'})
        restored = SandboxSession('s', self.source, self.paths, self.backend, ResourceLimits(), free_space_floor_bytes=0)
        self.assertEqual(restored.authority, self.session.authority)

    def test_mutation_failure_quarantines_and_denies_harvest(self):
        self.session.create()
        self.session.start()
        self.backend.fs_call = lambda request, grant: {'success': False, 'status': 'runtime_error', 'error': 'disk full'}
        store = EventStore(self.root / 'events.jsonl', session_id='s')
        self.addCleanup(store.close)
        tool = WriteTool(self.session)
        executor = ToolExecutor(store, get_tool=lambda name: tool, policy=lambda execution: 'allow',
                                get_security_context=self.session.security_context)
        execution_id = executor.request_batch([call(name='write', arguments='{"path":"main.py","content":"after"}')], 't')[0]
        self.assertFalse(executor.execute(execution_id).success)
        self.assertTrue(self.session.quarantined)
        self.assertEqual(self.session.security_context(), {})
        with self.assertRaisesRegex(SessionError, 'recovery_required'):
            self.session.prepare_changes()

    def test_excluded_and_absent_control_paths(self):
        self.session.create()
        self.session.start()
        store = EventStore(self.root / 'events.jsonl', session_id='s')
        self.addCleanup(store.close)
        tool = WriteTool(self.session)
        executor = ToolExecutor(store, get_tool=lambda name: tool, policy=lambda execution: 'allow',
                                get_security_context=self.session.security_context)
        for path in ('.env', '.git/config', 'pkg/client_credentials.json'):
            arguments = __import__('json').dumps({'path': path, 'content': 'x'})
            result = executor.execute(executor.request_batch([call(name='write', arguments=arguments)], 't')[0])
            self.assertFalse(result.success)
            self.assertFalse((self.paths.workspace / path).exists())
        result = executor.execute(executor.request_batch([call(name='write', arguments='{"path":".github/workflows/new.yml","content":"new"}')], 't')[0])
        self.assertFalse(result.success)
        self.assertFalse((self.paths.workspace / '.github').exists())
        self.assertEqual(list(replay(store.read_all()).executions.values())[-1].status, ToolExecutionStatus.WAITING_APPROVAL)

    def test_approval_presentation_uses_sealed_before_content(self):
        from agent.loop import Agent
        self.session.create()
        self.session.start()
        agent = Agent(str(self.root / 's.jsonl'), workdir=str(self.source), sandbox=self.session)
        self.addCleanup(agent.close)
        execution_id = agent.tool_executor.request_batch([call(name='write', arguments='{"path":"Makefile","content":"build"}')], 't')[0]
        agent.tool_executor.execute(execution_id)
        agent._refresh()
        presentation = agent.approval_presentation(execution_id)
        self.assertEqual(presentation['before']['kind'], 'absent')
        self.assertEqual(presentation['after_content'], 'build')
        self.assertTrue(presentation['binding_digest'])

    def test_ordinary_html_request_recovers_from_opaque_inspection(self):
        from agent.loop import Agent
        from main import run_repl
        from runtime.safety import SafetyConfig
        from tests.fakes import stream_text, stream_tool_call
        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        verifier = self.backend.verify_file_adapter
        with patch.object(self.backend, 'verify_file_adapter',
                          side_effect=lambda *args: verifier(*args) | {'opaque_ro': True}):
            self.session.create(); self.session.start()
        events = self.root / 's.jsonl'
        agent = Agent(str(events), workdir=str(self.source), sandbox=self.session,
                      safety_config=SafetyConfig(approval_mode='ask_all'))
        messages = []
        inputs = iter(['create a milk tea website', '1', 'exit'])
        prompts = []
        def read_input(prompt):
            prompts.append(prompt)
            return next(inputs)
        inspect = stream_tool_call('inspect', 'bash',
            '{"command":"ls -la && find . -maxdepth 2 -type f | sed \'s#^./##\' | head -100","cwd":".","verification_kind":"structural"}')
        write = stream_tool_call('write', 'write', '{"path":"index.html","content":"<h1>Milk tea</h1>"}')
        with patch('agent.loop.llm.complete', side_effect=[inspect, write, stream_text('created')]) as model:
            run_repl(events, agent_factory=lambda path: agent, input_fn=read_input, print_fn=messages.append)
        self.assertEqual(sum('Approval decision' in prompt for prompt in prompts), 1)
        self.assertEqual((self.paths.workspace / 'index.html').read_text(), '<h1>Milk tea</h1>')
        self.assertFalse((self.source / 'index.html').exists())
        events = agent.event_store.read_all()
        self.assertFalse(replay(events).security_state.secret_exposure_detected)
        bash = next(item for item in replay(events).executions.values() if item.tool_name == 'bash')
        self.assertEqual(bash.status, ToolExecutionStatus.CANCELLED)
        self.assertFalse(any(event['event_type'] in {'ToolStarted', 'ToolApprovalRequested', 'ToolGrantClaimed'}
                             and event['aggregate_id'] == bash.execution_id for event in events))
        correction = model.call_args_list[1].kwargs['messages']
        self.assertIn('write/edit', str(correction))
        self.assertIn('fixture.txt', str(correction))
        self.assertNotIn('canary-value', str(correction))
        self.assertNotIn('canary-value', '\n'.join(messages))

    def test_opaque_secret_preflight_blocks_callbacks_in_all_modes_without_tainting(self):
        from runtime.safety import SafetyConfig
        from tools.terminal import BashTool
        from unittest.mock import Mock
        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        verifier = self.backend.verify_file_adapter
        with patch.object(self.backend, 'verify_file_adapter',
                          side_effect=lambda *args: verifier(*args) | {'opaque_ro': True}):
            self.session.create(); self.session.start()
        for mode in ('ask_all', 'ask_on_escalation', 'auto_review'):
            with self.subTest(mode=mode), EventStore(self.root / (mode + '.jsonl'), session_id='s') as store:
                from runtime.models import ApprovalDecision
                approval, reviewer = Mock(return_value=ApprovalDecision(False)), Mock()
                executor = ToolExecutor(store, get_tool=lambda name: BashTool(self.session),
                    policy=lambda execution: 'allow', get_security_context=self.session.security_context,
                    safety_config=SafetyConfig(approval_mode=mode), approval_handler=approval, reviewer=reviewer)
                identity = executor.request_batch([call(name='bash', arguments='{"command":"python main.py"}')], 't')[0]
                result = executor.execute(identity)
                self.assertEqual(result.metadata.get('reason_code'), 'opaque_inputs_not_disclosable')
                approval.assert_not_called(); reviewer.review.assert_not_called()
                self.assertFalse(replay(store.read_all()).security_state.secret_exposure_detected)
                self.assertFalse(any(event['event_type'] in {'ToolStarted', 'ToolGrantClaimed', 'ToolApprovalRequested'}
                                     for event in store.read_all()))

    def test_clean_opaque_request_still_requires_approval(self):
        from runtime.safety import SafetyConfig
        from tools.terminal import BashTool
        verifier = self.backend.verify_file_adapter
        with patch.object(self.backend, 'verify_file_adapter',
                          side_effect=lambda *args: verifier(*args) | {'opaque_ro': True}):
            self.session.create(); self.session.start()
        with EventStore(self.root / 'clean.jsonl', session_id='s') as store:
            executor = ToolExecutor(store, get_tool=lambda name: BashTool(self.session),
                policy=lambda execution: 'allow', get_security_context=self.session.security_context,
                safety_config=SafetyConfig(approval_mode='ask_all'))
            identity = executor.request_batch([call(name='bash', arguments='{"command":"python main.py"}')], 't')[0]
            executor.execute(identity)
            state = replay(store.read_all())
            self.assertEqual(state.executions[identity].status, ToolExecutionStatus.WAITING_APPROVAL)
            self.assertEqual(self.session.approval_preview(state.executions[identity])['sealed_inputs'], {'main.py': 'before'})
            self.assertFalse(executor.resolve_approval(identity, False, 'test rejection').success)
            self.assertFalse(any(event['event_type'] in {'ToolStarted', 'ToolGrantClaimed'} for event in store.read_all()))

    def test_actual_secret_read_still_blocks_session(self):
        from tools.filesystem import ReadTool
        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        self.session.create(); self.session.start()
        with EventStore(self.root / 'read.jsonl', session_id='s') as store:
            executor = ToolExecutor(store, get_tool=lambda name: ReadTool(self.session),
                policy=lambda execution: 'allow', get_security_context=self.session.security_context)
            identity = executor.request_batch([call(name='read', arguments='{"path":"fixture.txt"}')], 't')[0]
            result = executor.execute(identity)
            self.assertEqual(result.metadata.get('reason_code'), 'disclosure_denied')
            self.assertTrue(replay(store.read_all()).security_state.secret_exposure_detected)
            self.assertNotIn('canary-value', (self.root / 'read.jsonl').read_text())

    def test_ask_all_cli_can_approve_or_reject_targeted_html_write(self):
        from agent.loop import Agent
        from main import run_repl
        from runtime.safety import SafetyConfig
        from tests.fakes import stream_text, stream_tool_call
        (self.source / 'fixture.txt').write_text('SECRET=canary-value')
        self.session.create(); self.session.start()
        events = self.root / 's.jsonl'
        agent = Agent(str(events), workdir=str(self.source), sandbox=self.session,
                      safety_config=SafetyConfig(approval_mode='ask_all'))
        content = '<h1>Test ask_all</h1>'
        choices = iter(['create html', '1', 'create second html', '2', 'exit'])
        prompts, messages = [], []
        def read_input(prompt):
            prompts.append(prompt)
            return next(choices)
        first = stream_tool_call('first', 'write', '{"path":"demo.html","content":"<h1>Test ask_all</h1>"}')
        second = stream_tool_call('second', 'write', '{"path":"rejected.html","content":"rejected"}')
        with patch('agent.loop.llm.complete', side_effect=[first, stream_text('created'), second, stream_text('rejected')]):
            run_repl(events, agent_factory=lambda path: agent, input_fn=read_input, print_fn=messages.append)
        self.assertEqual(sum('Approval decision' in prompt for prompt in prompts), 2)
        self.assertEqual((self.paths.workspace / 'demo.html').read_text(), content)
        self.assertFalse((self.paths.workspace / 'rejected.html').exists())
        self.assertFalse((self.source / 'demo.html').exists())
        self.assertFalse(replay(agent.event_store.read_all()).security_state.secret_exposure_detected)
