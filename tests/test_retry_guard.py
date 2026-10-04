import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.event_store import EventStore
from runtime.models import PlanMode
from runtime.reducer import runtime_state_frame, replay
from tests.fakes import FakeTool, safety_context, stream_text, stream_tool_call, trusted_agent, trusted_executor
from tests.test_tool_lifecycle import call
from tools.filesystem import WriteTool
from tools.terminal import BashTool


class RetryGuardTests(unittest.TestCase):
    def test_ignored_guard_stops_variants_before_max_iterations(self):
        with tempfile.TemporaryDirectory() as folder:
            bash = FakeTool()
            bash.name, bash.parameters = 'bash', BashTool().parameters

            class Registry:
                def schemas(self):
                    return []

                def get(self, name):
                    return bash if name == 'bash' else None

            context = safety_context('events') | {
                'opaque_input_preview': lambda digest: {'fixture.txt': 'SECRET=canary-value'},
            }
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder,
                                  tool_registry=Registry(), get_security_context=lambda: context)
            self.addCleanup(agent.close)
            counter = 0

            def respond(**kwargs):
                nonlocal counter
                counter += 1
                return stream_tool_call(str(counter), 'bash', json.dumps({'command': f'python main.py --try{counter}'}))

            with patch('agent.loop.llm.complete', side_effect=respond):
                self.assertEqual(agent.run_turn('create html'), '')
            self.assertEqual(counter, 4)
            self.assertEqual(agent.turn_state.error.category, 'repeated_policy_retry')
            self.assertEqual(bash.calls, 0)

    def test_variants_of_same_denied_shell_intent_are_forbidden_but_write_can_continue(self):
        with tempfile.TemporaryDirectory() as folder, EventStore(Path(folder) / 'events.jsonl') as store:
            bash, write = FakeTool(), FakeTool()
            bash.name, bash.parameters = 'bash', BashTool().parameters
            write.name, write.parameters = 'write', WriteTool.parameters
            store.append_event('TurnStarted', 'turn', 'turn', {'goal': 'create html'}, turn_id='turn')
            context = safety_context(store.session_id) | {
                'opaque_input_preview': lambda digest: {'fixture.txt': 'SECRET=canary-value'},
            }
            executor = trusted_executor(store, get_tool={'bash': bash, 'write': write}.get,
                                        get_security_context=lambda: context)
            results = []
            for arguments in ({'command': 'python main.py'}, {'command': 'python main.py --check'},
                              {'command': 'python main.py --verify', 'cwd': 'tests', 'verification_kind': 'behavioral'}):
                identity = executor.request_batch([call(name='bash', arguments=json.dumps(arguments))], 'turn')[0]
                results.append(executor.execute(identity))
            self.assertEqual([result.metadata.get('reason_code') for result in results],
                             ['opaque_inputs_not_disclosable', 'opaque_inputs_not_disclosable', 'retry_forbidden'])
            frame = runtime_state_frame(replay(store.read_all()))
            self.assertEqual(frame['retry_constraints'][0]['reason_code'], 'opaque_inputs_not_disclosable')
            self.assertEqual(frame['retry_constraints'][0]['attempts'], 2)
            self.assertEqual(bash.calls, 0)
            identity = executor.request_batch([call(name='write', arguments='{"path":"index.html","content":"<h1>Tea</h1>"}')], 'turn')[0]
            self.assertTrue(executor.execute(identity).success)
            self.assertEqual(write.calls, 1)
            self.assertEqual(runtime_state_frame(replay(store.read_all()))['retry_constraints'], [])

    def test_loop_survives_denial_and_chooses_file_tool_without_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            bash, write = FakeTool(), FakeTool()
            bash.name, bash.parameters = 'bash', BashTool().parameters
            write.name, write.parameters = 'write', WriteTool.parameters

            class Registry:
                def schemas(self):
                    return []

                def get(self, name):
                    return {'bash': bash, 'write': write}.get(name)

            context = safety_context('events') | {
                'opaque_input_preview': lambda digest: {'fixture.txt': 'SECRET=canary-value'},
            }
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder,
                                  tool_registry=Registry(), get_security_context=lambda: context)
            self.addCleanup(agent.close)
            responses = iter([
                stream_tool_call('first', 'bash', '{"command":"python main.py"}'),
                stream_tool_call('second', 'bash', '{"command":"python main.py"}'),
                stream_tool_call('third', 'bash', '{"command":"python main.py --verify"}'),
                stream_tool_call('write', 'write', '{"path":"index.html","content":"<h1>Tea</h1>"}'),
                stream_text('created'),
            ])

            def respond(**kwargs):
                if agent.turn_state.iteration == 3:
                    self.assertIn('retry_constraints', kwargs['messages'][0]['content'])
                    self.assertIn('opaque_inputs_not_disclosable', kwargs['messages'][0]['content'])
                return next(responses)

            with patch('agent.loop.llm.complete', side_effect=respond):
                self.assertEqual(agent.run_turn('create html'), 'created')
            self.assertEqual(write.calls, 1)
            self.assertEqual(bash.calls, 0)
            self.assertIsNone(agent.turn_state.plan_id)

    def test_no_plan_state_lists_regular_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = trusted_agent(str(Path(folder) / 'events.jsonl'), workdir=folder)
            self.addCleanup(agent.close)
            agent._event('TurnStarted', 'turn', 'turn', {'goal': 'create html', 'plan_mode': PlanMode.OPTIONAL.value}, turn_id='turn')
            actions = {item['action'] for item in runtime_state_frame(agent.runtime_state)['allowed_actions']}
            self.assertTrue({'read', 'write', 'edit', 'bash'}.issubset(actions))
