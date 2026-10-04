import json
import tempfile
import unittest
from pathlib import Path

from memory.event_store import EventStore
from model import llm
from runtime.safety import DisclosureDenied, ReviewVerdict, SafetyConfig
from tests.fakes import FakeTool, safety_context, trusted_executor
from tests.test_tool_lifecycle import call
from tools.filesystem import WriteTool
from tools.terminal import BashTool


class PolicyTraceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = EventStore(self.root / 'events.jsonl')
        self.addCleanup(self.store.close)
        self.trace = self.root / '.llm-traces' / 'turn.txt'
        llm.start_trace(self.trace)
        self.addCleanup(llm.finish_trace)

    def records(self):
        return [json.loads(line.removeprefix('[runtime] '))
                for line in self.trace.read_text().splitlines() if line.startswith('[runtime] ')]

    def test_file_allow_and_shell_review_include_command_path_cwd_and_reason(self):
        write, bash = FakeTool(), FakeTool()
        write.name, write.parameters = 'write', WriteTool.parameters
        bash.name, bash.parameters = 'bash', BashTool().parameters
        context = safety_context(self.store.session_id) | {
            'opaque_input_preview': lambda generation: {'src/a.py': 'hello'},
            'trusted_analysis': {'complete': True},
        }

        class Reviewer:
            def review(self, facts):
                return ReviewVerdict('allow', 'reviewer_allow')

        executor = trusted_executor(
            self.store, get_tool={'write': write, 'bash': bash}.get,
            get_security_context=lambda: context, reviewer=Reviewer(),
            safety_config=SafetyConfig(approval_mode='auto_review'),
        )
        for name, arguments in (
            ('write', {'path': 'src/a.py', 'content': 'hello'}),
            ('bash', {'command': 'python -m unittest', 'cwd': 'src'}),
        ):
            identity = executor.request_batch([call(name=name, arguments=json.dumps(arguments))], 'turn')[0]
            self.assertTrue(executor.execute(identity).success)

        records = self.records()
        hard = [record for record in records if record['event'] == 'hard_policy'
                and record['phase'] == 'authorization']
        self.assertEqual([(r['tool_name'], r['decision'], r['reason_code']) for r in hard],
                         [('write', 'allow', 'scoped_file'), ('bash', 'review', 'project_execution')])
        self.assertEqual(hard[0]['path'], 'src/a.py')
        self.assertNotIn('hello', self.trace.read_text())
        self.assertEqual(hard[1]['command'], 'python -m unittest')
        self.assertEqual(hard[1]['cwd'], 'src')
        routes = [record for record in records if record['event'] == 'approval_route']
        self.assertEqual(routes[-1]['route'], 'execute')
        self.assertEqual(routes[-1]['reviewer'], {'decision': 'allow', 'reason_code': 'reviewer_allow'})
        self.assertEqual(write.calls, 1)
        self.assertEqual(bash.calls, 1)

    def test_hard_deny_is_traced_before_tool_effect(self):
        tool = FakeTool()
        context = safety_context(self.store.session_id)
        context['capabilities']['internal_tools'] = ()
        executor = trusted_executor(self.store, get_tool=lambda name: tool,
                                    get_security_context=lambda: context)
        identity = executor.request_batch([call()], 'turn')[0]
        self.assertFalse(executor.execute(identity).success)
        hard = [record for record in self.records() if record['event'] == 'hard_policy']
        self.assertEqual(hard[0]['decision'], 'deny')
        self.assertEqual(hard[0]['reason_code'], 'forbidden_operation')
        self.assertEqual(tool.calls, 0)

    def test_trace_disclosure_denial_does_not_leak_or_change_execution(self):
        def deny_trace(content, sink):
            if sink == 'trace':
                raise DisclosureDenied('missing_disclosure_authority')

        context = safety_context(self.store.session_id) | {'disclosure_check': deny_trace}
        tool = FakeTool()
        executor = trusted_executor(self.store, get_tool=lambda name: tool,
                                    get_security_context=lambda: context)
        identity = executor.request_batch([call()], 'turn')[0]
        self.assertTrue(executor.execute(identity).success)
        self.assertEqual(self.trace.read_text(), '')
        self.assertEqual(tool.calls, 1)
