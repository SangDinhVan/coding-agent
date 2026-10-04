import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.event_store import EventStore
from runtime.executor import ToolExecutor
from runtime.models import ApprovalDecision, PolicyDecision, ToolExecutionStatus
from runtime.reducer import replay
from runtime.safety import AuthorityRecord, SafetyConfig
from tests.fakes import FakeTool
from tests.test_tool_lifecycle import call


def trusted_context(store, **overrides):
    return {
        'authority': AuthorityRecord('auth', store.session_id, 'a' * 64, 'private', ('fake',), ('ui', 'main_model', 'reviewer', 'summarizer', 'memory', 'audit_text', 'trace')),
        'generation_digest': 'b' * 64, 'scope': 'private',
        'image_digest': 'sha256:' + 'c' * 64, 'environment_digest': 'd' * 64,
        'capabilities': {'profile_id': 'shadow', 'scope': 'private', 'internal_tools': ('fake',)},
        'security_state': {'security_state_version': 0},
        'checkpoint_action': lambda action: 'e' * 64,
        **overrides,
    }


class SafetyExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = EventStore(Path(directory.name) / 'session.jsonl')
        self.addCleanup(self.store.close)
        self.tool = FakeTool()
        self.context = trusted_context(self.store)

    def executor(self, **kwargs):
        return ToolExecutor(self.store, get_tool=lambda name: self.tool,
                            get_security_context=lambda: self.context, **kwargs)

    def test_policy_is_mandatory(self):
        for policy in (None, lambda execution: 1 / 0):
            executor = self.executor(policy=policy)
            result = executor.execute(executor.request_batch([call()], 't')[0])
            self.assertFalse(result.success)
            self.assertEqual(result.metadata['reason_code'], 'policy_unavailable')
        self.assertEqual(self.tool.calls, 0)

    def test_approval_rechecks_for_callback_and_resume(self):
        def approve(request):
            self.context['generation_digest'] = 'f' * 64
            return ApprovalDecision(True)
        executor = self.executor(policy=lambda execution: 'ask', approval_handler=approve)
        result = executor.execute(executor.request_batch([call()], 't')[0])
        self.assertFalse(result.success)
        self.assertEqual(self.tool.calls, 0)
        executor = self.executor(policy=lambda execution: 'ask')
        execution_id = executor.request_batch([call()], 't')[0]
        executor.execute(execution_id)
        self.context['security_state'] = {'security_state_version': 1}
        result = executor.resolve_approval(execution_id, True, 'yes')
        self.assertFalse(result.success)
        self.assertEqual(self.tool.calls, 0)

    def test_same_binding_ask_all_and_one_use(self):
        approvals = []
        executor = self.executor(policy=lambda execution: 'allow', safety_config=SafetyConfig(approval_mode='ask_all'),
                                 approval_handler=lambda request: (approvals.append(request) or ApprovalDecision(True)))
        execution_id = executor.request_batch([call()], 't')[0]
        self.assertTrue(executor.execute(execution_id).success)
        self.assertFalse(executor.execute(execution_id).success)
        self.assertEqual(self.tool.calls, 1)
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approvals[0].binding_digest)
        self.assertTrue(replay(self.store.read_all()).executions[execution_id].grant_claimed)

    def test_unbound_legacy_approval_regates(self):
        executor = self.executor(policy=lambda execution: 'ask')
        execution_id = executor.request_batch([call()], 't')[0]
        self.store.append_event('ToolApprovalRequested', 'tool_execution', execution_id, {}, turn_id='t')
        self.store.append_event('ToolApproved', 'tool_execution', execution_id, {'approved_by': 'user'}, turn_id='t')
        result = executor.execute(execution_id)
        self.assertFalse(result.success)
        self.assertEqual(self.tool.calls, 0)
        self.assertEqual(replay(self.store.read_all()).executions[execution_id].status, ToolExecutionStatus.WAITING_APPROVAL)

    def test_checkpoint_or_intent_failure_has_no_effects(self):
        executor = self.executor(policy=lambda execution: 'allow')
        self.context['checkpoint_action'] = lambda action: None
        self.assertFalse(executor.execute(executor.request_batch([call()], 't')[0]).success)
        self.assertEqual(self.tool.calls, 0)
        self.context = trusted_context(self.store)
        execution_id = executor.request_batch([call()], 't')[0]
        original = self.store.append_event
        def fail(kind, *args, **kwargs):
            if kind == 'ToolStarted':
                raise OSError('disk full')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'append_event', side_effect=fail), self.assertRaises(OSError):
            executor.execute(execution_id)
        self.assertEqual(self.tool.calls, 0)
        self.assertEqual(replay(self.store.read_all()).executions[execution_id].status, ToolExecutionStatus.RECOVERY_REQUIRED)

    def test_post_effect_audit_failure_prevents_retry(self):
        executor = self.executor(policy=lambda execution: 'allow')
        execution_id = executor.request_batch([call()], 't')[0]
        original = self.store.append_event
        def fail(kind, *args, **kwargs):
            if kind == 'ToolCompleted':
                raise OSError('disk full')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'append_event', side_effect=fail), self.assertRaises(OSError):
            executor.execute(execution_id)
        self.assertFalse(executor.execute(execution_id).success)
        self.assertEqual(self.tool.calls, 1)
        next_id = executor.request_batch([call('next')], 't')[0]
        self.assertFalse(executor.execute(next_id).success)
        self.assertEqual(self.tool.calls, 1)

    def test_headless_has_no_waiting_approval(self):
        executor = self.executor(policy=lambda execution: 'ask', safety_config=SafetyConfig(interaction_mode='headless'))
        result = executor.execute(executor.request_batch([call()], 't')[0])
        self.assertEqual(result.metadata['reason_code'], 'human_approval_unavailable')
        self.assertFalse(any(event['event_type'] == 'ToolApprovalRequested' for event in self.store.read_all()))

    def test_policy_flip_blocks_callback_and_resume(self):
        decision = ['ask']
        def approve(request):
            decision[0] = 'deny'
            return ApprovalDecision(True)
        executor = self.executor(policy=lambda execution: decision[0], approval_handler=approve)
        self.assertFalse(executor.execute(executor.request_batch([call()], 't')[0]).success)
        decision[0] = 'ask'
        executor = self.executor(policy=lambda execution: decision[0])
        execution_id = executor.request_batch([call()], 't')[0]
        executor.execute(execution_id)
        decision[0] = 'deny'
        self.assertFalse(executor.resolve_approval(execution_id, True, 'yes').success)
        self.assertEqual(self.tool.calls, 0)

    def test_reviewer_only_for_hard_review_auto_mode(self):
        from unittest.mock import Mock
        from runtime.safety import ReviewVerdict
        reviewer = Mock()
        reviewer.review.return_value = ReviewVerdict('allow', 'reviewer_allow')
        for mode in ('ask_all', 'ask_on_escalation', 'auto_review'):
            for decision in ('allow', 'ask', 'deny'):
                executor = self.executor(policy=lambda execution, d=decision: d, reviewer=reviewer,
                                         safety_config=SafetyConfig(approval_mode=mode, interaction_mode='headless'))
                executor.execute(executor.request_batch([call()], 't')[0])
        reviewer.review.assert_not_called()

    def test_incomplete_analysis_has_no_reviewer_call(self):
        from unittest.mock import Mock
        reviewer = Mock()
        executor = self.executor(policy=lambda execution: 'review', reviewer=reviewer,
                                 safety_config=SafetyConfig(approval_mode='auto_review'))
        executor.execute(executor.request_batch([call()], 't')[0])
        reviewer.review.assert_not_called()

    def test_reviewer_allow_rechecks_and_failure_preserves_reason(self):
        from unittest.mock import Mock
        from runtime.safety import ReviewVerdict
        self.context['trusted_analysis'] = {'complete': True}
        reviewer = Mock()
        def change(facts):
            self.context['generation_digest'] = 'f' * 64
            return ReviewVerdict('allow', 'reviewer_allow')
        reviewer.review.side_effect = change
        executor = self.executor(policy=lambda execution: 'review', reviewer=reviewer,
                                 safety_config=SafetyConfig(approval_mode='auto_review'))
        self.assertFalse(executor.execute(executor.request_batch([call()], 't')[0]).success)
        self.assertEqual(self.tool.calls, 0)
        reviewer.review.side_effect = None
        reviewer.review.return_value = ReviewVerdict('ask', 'reviewer_timeout')
        executor = self.executor(policy=lambda execution: 'review', reviewer=reviewer,
                                 safety_config=SafetyConfig(approval_mode='auto_review', interaction_mode='headless'))
        result = executor.execute(executor.request_batch([call()], 't')[0])
        self.assertEqual(result.metadata['reason_code'], 'human_approval_unavailable')
        self.assertEqual(result.metadata['review_reason_code'], 'reviewer_timeout')

    def test_recovery_retry_gets_new_identity_and_grant(self):
        from runtime.models import RecoveryDecision, ReplayPolicy
        self.tool.replay_policy = ReplayPolicy.REPLAY_SAFE
        self.tool.error = SystemExit(2)
        executor = self.executor(policy=lambda execution: 'allow')
        original_id = executor.request_batch([call()], 't')[0]
        with self.assertRaises(SystemExit):
            executor.execute(original_id)
        self.tool.error = None
        self.assertTrue(executor.resolve_recovery(original_id, RecoveryDecision.RETRY, 'authenticated retry').success)
        state = replay(self.store.read_all())
        self.assertEqual(state.executions[original_id].status, ToolExecutionStatus.FAILED)
        self.assertEqual(len(state.executions), 2)
        retried = list(state.executions.values())[-1]
        self.assertEqual(retried.retry_of_execution_id, original_id)
        self.assertTrue(retried.grant_claimed)
        self.assertNotEqual(retried.execution_id, original_id)
