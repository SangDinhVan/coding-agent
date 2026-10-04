import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime.safety import CoreLLMReviewer


def response(body, tools=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body, tool_calls=tools))])


class CoreReviewerTests(unittest.TestCase):
    def test_missing_disclosure_gate_calls_no_provider(self):
        with patch('model.llm.complete') as complete:
            self.assertEqual(CoreLLMReviewer().review(self.facts).decision, 'ask')
            complete.assert_not_called()

    facts = {'goal': 'inspect source', 'analysis_complete': True,
             'action': {'tool_name': 'bash', 'cwd': '.', 'argument_digest': 'a' * 64, 'generation_digest': 'b' * 64},
             'scope': {'profile_id': 'shadow', 'capabilities': {'opaque_ro': True}},
             'security_state': {'security_state_version': 0}}

    def test_isolated_request(self):
        reviewer = CoreLLMReviewer(model='selected', base_url='https://provider.invalid', api_key='key', disclosure_check=lambda text,sink: text)
        with patch('model.llm.complete', return_value=response('{"decision":"allow","reason_code":"reviewer_allow"}')) as complete:
            self.assertEqual(reviewer.review(self.facts).decision, 'allow')
        request = complete.call_args.kwargs
        self.assertEqual(request['model'], 'selected')
        self.assertEqual(request['base_url'], 'https://provider.invalid')
        self.assertIsNone(request['tools'])
        self.assertFalse(request['stream'])
        self.assertEqual(request['timeout'], 30)
        self.assertEqual(request['max_retries'], 0)
        self.assertEqual(len(request['messages']), 2)
        self.assertEqual(json.loads(request['messages'][1]['content']), self.facts)

    def test_strict_schema_and_failure(self):
        for body, tools in (('', None), ('{}', None),
                            ('{"decision":"ALLOW","reason_code":"reviewer_allow"}', None),
                            ('{"decision":"allow","reason_code":"free rationale"}', None),
                            ('{"decision":"allow","reason_code":"reviewer_allow","extra":1}', None),
                            ('{"decision":"allow","reason_code":"reviewer_allow"}', ['tool']),
                            ('{"decision":"deny","decision":"allow","reason_code":"reviewer_allow"}', None)):
            with self.subTest(body=body), patch('model.llm.complete', return_value=response(body, tools)):
                verdict = CoreLLMReviewer(disclosure_check=lambda text,sink: text).review(self.facts)
                self.assertEqual(verdict.decision, 'ask')
                self.assertEqual(verdict.reason_code, 'reviewer_invalid_response')
        for error, reason in ((TimeoutError(), 'reviewer_timeout'), (RuntimeError('secret'), 'reviewer_error')):
            with patch('model.llm.complete', side_effect=error):
                verdict = CoreLLMReviewer(disclosure_check=lambda text,sink: text).review(self.facts)
                self.assertEqual((verdict.decision, verdict.reason_code), ('ask', reason))
        with patch('model.llm.complete') as complete:
            verdict = CoreLLMReviewer().review(self.facts | {'analysis_complete': False})
            self.assertEqual(verdict.decision, 'ask')
            complete.assert_not_called()
            CoreLLMReviewer().review(self.facts | {'stdout': 'untrusted'})
            complete.assert_not_called()
