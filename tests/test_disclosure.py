import unittest
from unittest.mock import Mock, patch

from runtime.models import SecurityState
from runtime.safety import AuthorityRecord, DisclosureDenied, DisclosureGate


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.gate = DisclosureGate(known_secrets=('CANARY_SECRET_VALUE',))
        self.authority = AuthorityRecord('a', 's', 'a'*64, 'scope', ('read',), tuple(DisclosureGate.SINKS))

    def test_each_sink_before_side_effect(self):
        for sink in self.gate.SINKS:
            with self.subTest(sink=sink):
                for content,state,authority,provenance in [
                    ('CANARY_SECRET_VALUE', SecurityState(), self.authority, ('g',)),
                    ('benign', SecurityState(secret_exposure_detected=True), self.authority, ('g',)),
                    ('benign', SecurityState(), None, ('g',)),
                    ('benign', SecurityState(), self.authority, ()),
                ]:
                    with self.assertRaises(DisclosureDenied):
                        self.gate.check(content,sink,authority,state,provenance)
        self.assertEqual(self.gate.check('benign','ui',self.authority,SecurityState(),('g',)), 'benign')

    def test_model_gate_runs_before_provider(self):
        from model import llm
        with patch('model.llm.OpenAI') as provider:
            with self.assertRaises(DisclosureDenied):
                llm.complete(messages=[{'role':'user','content':'secret'}], disclosure_check=Mock(side_effect=DisclosureDenied('secret_exposure')), disclosure_sink='main_model')
            provider.assert_not_called()

    def test_streaming_canary_never_reaches_ui_journal_or_memory(self):
        import tempfile
        from pathlib import Path
        from tests.fakes import trusted_agent, stream_text
        from runtime.reducer import replay
        with tempfile.TemporaryDirectory() as folder:
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder)
            self.addCleanup(agent.close)
            agent.disclosure_gate = self.gate
            with patch('agent.loop.llm.complete', return_value=stream_text('CANARY_SECRET_VALUE')), patch('builtins.print') as output:
                with self.assertRaises(DisclosureDenied):
                    agent.run_turn('goal')
            self.assertNotIn('CANARY_SECRET_VALUE', str(output.call_args_list))
            self.assertNotIn('CANARY_SECRET_VALUE', agent.event_store.path.read_text())
            self.assertTrue(replay(agent.event_store.read_all()).security_state.secret_exposure_detected)
            with self.assertRaises(DisclosureDenied):
                agent.memory_manager.remember('benign')
            for trace in (Path(folder) / '.llm-traces').rglob('*.txt'):
                self.assertNotIn('CANARY_SECRET_VALUE', trace.read_text())

    def test_secret_arguments_stop_tools_and_provider_before_call(self):
        import tempfile
        from pathlib import Path
        from tests.fakes import trusted_agent
        with tempfile.TemporaryDirectory() as folder:
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder)
            self.addCleanup(agent.close)
            agent.disclosure_gate = self.gate
            with patch('agent.loop.llm.complete') as provider:
                with self.assertRaises(DisclosureDenied):
                    agent.run_turn('CANARY_SECRET_VALUE')
            provider.assert_not_called()
            self.assertNotIn('CANARY_SECRET_VALUE', agent.event_store.path.read_text())

    def test_audit_metadata_cannot_hide_canary(self):
        import tempfile
        from pathlib import Path
        from tests.fakes import trusted_agent
        with tempfile.TemporaryDirectory() as folder:
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder)
            self.addCleanup(agent.close)
            agent.disclosure_gate = self.gate
            agent.event_store.append_event('ContextCompacted', 'context', 's', {'metadata': {'unexpected': 'CANARY_SECRET_VALUE'}})
            self.assertNotIn('CANARY_SECRET_VALUE', agent.event_store.path.read_text())

    def test_provider_exception_never_escapes_with_secret_text(self):
        import tempfile
        from pathlib import Path
        from tests.fakes import trusted_agent
        with tempfile.TemporaryDirectory() as folder:
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder)
            self.addCleanup(agent.close)
            agent.disclosure_gate = self.gate
            with patch('agent.loop.llm.complete', side_effect=RuntimeError('CANARY_SECRET_VALUE')):
                with self.assertRaises(RuntimeError) as caught:
                    agent.run_turn('goal')
            self.assertNotIn('CANARY_SECRET_VALUE', str(caught.exception))
            self.assertNotIn('CANARY_SECRET_VALUE', agent.event_store.path.read_text())

    def test_standalone_executor_never_audits_secret_arguments(self):
        import tempfile
        from pathlib import Path
        from memory.event_store import EventStore
        from tests.fakes import FakeTool, trusted_executor
        from tests.test_tool_lifecycle import call
        with tempfile.TemporaryDirectory() as folder, EventStore(Path(folder) / 'events.jsonl') as store:
            tool = FakeTool()
            executor = trusted_executor(store, get_tool=lambda name: tool)
            identity = executor.request_batch([call(arguments='{"content":"SECRET=canary-value"}')], 't')[0]
            self.assertFalse(executor.execute(identity).success)
            self.assertNotIn('SECRET=canary-value', store.path.read_text())
