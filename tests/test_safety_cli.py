import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.loop import Agent
from main import build_parser, handle_pending_runtime_actions, run_headless, select_chat_path
from runtime.safety import SafetyConfig
from tests.fakes import FakeTool, stream_text
from tests.test_recovery import FakeRegistry
from tests.test_safety_execution import trusted_context


class ApprovalCliTests(unittest.TestCase):
    def test_defaults_and_host_constraints(self):
        args = build_parser().parse_args(['--image', 'sha256:' + 'a' * 64])
        self.assertIsNone(args.approval_mode)
        self.assertFalse(args.headless)
        with tempfile.TemporaryDirectory() as folder:
            config = SafetyConfig(approval_mode='ask_all')
            agent = Agent(str(Path(folder) / 'events.jsonl'), workdir=folder, safety_config=config)
            self.addCleanup(agent.close)
            self.assertEqual(agent.tool_executor.safety_config.approval_mode, config.approval_mode)

    def test_headless_never_reads_input(self):
        args = build_parser().parse_args(['--image', 'sha256:' + 'a' * 64, '--headless', 'resume'])
        with self.assertRaisesRegex(ValueError, 'session_selection_required'):
            select_chat_path(args, input_fn=lambda prompt: self.fail('stdin'))
        with tempfile.TemporaryDirectory() as folder:
            agent = Agent(str(Path(folder) / 'events.jsonl'), workdir=folder,
                          safety_config=SafetyConfig(interaction_mode='headless'))
            self.addCleanup(agent.close)
            agent.event_store.append_event('ToolRequested', 'tool_execution', 'old',
                                          {'tool_call_id': 'call', 'tool_name': 'read', 'arguments': {}, 'replay_policy': 'manual'}, turn_id='t')
            agent.event_store.append_event('ToolApprovalRequested', 'tool_execution', 'old', {}, turn_id='t')
            agent._refresh()
            with self.assertRaisesRegex(RuntimeError, 'human_approval_unavailable'):
                handle_pending_runtime_actions(agent, input_fn=lambda prompt: self.fail('stdin'))
            with self.assertRaisesRegex(RuntimeError, 'human_approval_unavailable'):
                run_headless(agent, 'goal')

    def test_post_turn_approval_drains_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = FakeTool()
            agent = Agent(str(Path(folder) / 'events.jsonl'), workdir=folder,
                          policy=lambda execution: 'ask', tool_registry=FakeRegistry(tool),
                          get_security_context=lambda: trusted_context(agent.event_store))
            self.addCleanup(agent.close)
            calls = [SimpleNamespace(index=i, id=f'c{i}', type='function',
                                    function=SimpleNamespace(name='fake', arguments='{}')) for i in range(2)]
            first = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=calls))])]
            choices = iter(['invalid', '', '1', '3', 'two', '2'])
            prompts, outputs = [], []
            def read_input(prompt):
                prompts.append(prompt)
                if prompt == 'Approval note: ':
                    self.assertEqual(tool.calls, 1)
                    self.assertTrue(agent.pending_runtime_actions())
                try:
                    return next(choices)
                except StopIteration:
                    self.fail('Numeric choices must resolve approval without requiring another input')
            with patch('agent.loop.llm.complete', side_effect=[first, stream_text('done')]) as complete:
                agent.run_turn('goal')
                self.assertEqual(complete.call_count, 1)
                handle_pending_runtime_actions(agent, input_fn=read_input, print_fn=outputs.append)
                self.assertEqual(complete.call_count, 2)
            self.assertEqual(tool.calls, 1)
            executions = list(agent.runtime_state.executions.values())
            self.assertTrue(executions[0].approval.approved)
            self.assertEqual(executions[0].approval.note, '')
            self.assertFalse(executions[1].approval.approved)
            self.assertEqual(executions[1].approval.note, 'two')
            self.assertEqual(sum(prompt == 'Approval note: ' for prompt in prompts), 1)
            self.assertTrue(any('1: allow' in output and '2: deny' in output and '3: Note' in output for output in outputs))
            self.assertEqual([m['tool_call_id'] for m in agent.event_store.to_messages() if m['role'] == 'tool'], ['c0', 'c1'])

    def test_headless_human_gate_terminates_model_progress(self):
        from tests.fakes import stream_tool_call
        with tempfile.TemporaryDirectory() as folder:
            tool = FakeTool()
            agent = Agent(str(Path(folder) / 'events.jsonl'), workdir=folder,
                          tool_registry=FakeRegistry(tool),
                          safety_config=SafetyConfig(approval_mode='ask_all', interaction_mode='headless'),
                          get_security_context=lambda: trusted_context(agent.event_store))
            self.addCleanup(agent.close)
            with patch('agent.loop.llm.complete', return_value=stream_tool_call('call', 'fake')) as complete:
                agent.run_turn('goal')
                self.assertEqual(complete.call_count, 1)
            self.assertEqual(tool.calls, 0)
            self.assertEqual(agent.turn_state.error.category, 'human_approval_unavailable')
