import unittest
from dataclasses import replace

from runtime.models import PolicyDecision, ToolExecution, ToolExecutionStatus
from runtime.safety import AuthorityRecord, SafetyConfig, evaluate_policy, normalize_action
from tools.filesystem import WriteTool
from tools.terminal import BashTool


def execution(name='write', arguments=None):
    return ToolExecution('exec', 'call', 'session', 'turn', name,
                         arguments if arguments is not None else {'path': 'src/a.py', 'content': 'new'},
                         ToolExecutionStatus.PENDING, 'now')


def context():
    return {'generation_digest': 'a' * 64, 'before_digest': 'b' * 64,
            'path_roles': {'Makefile': 'project_control', 'controller': 'trusted'},
            'scope': 'private', 'image_digest': 'sha256:' + 'c' * 64,
            'environment_digest': 'd' * 64}


def authority():
    return AuthorityRecord('authority', 'session', 'e' * 64, 'private',
                           ('read', 'write', 'edit', 'bash'), ('main_model', 'ui'))


class SafetyPolicyTests(unittest.TestCase):
    def test_json_and_schema_boundary(self):
        for arguments in ([], None, 1, {'path': 'a', 'content': 1},
                          {'path': 'a', 'content': 'x', 'grant': {}},
                          {'path': '/tmp/a', 'content': 'x'},
                          {'path': '../a', 'content': 'x'}, {'path': 'a//b', 'content': 'x'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                item = execution()
                item.arguments = arguments
                normalize_action(item, WriteTool(), context())
        first = normalize_action(execution(), WriteTool(), context())
        same = normalize_action(execution(arguments={'content': 'new', 'path': 'src/a.py'}), WriteTool(), context())
        changed = normalize_action(execution(arguments={'content': 'other', 'path': 'src/a.py'}), WriteTool(), context())
        self.assertEqual(first.argument_digest, same.argument_digest)
        self.assertNotEqual(first.argument_digest, changed.argument_digest)
        with self.assertRaises(ValueError):
            normalize_action(execution('bash', {'command': 'true', 'verification_kind': 'bad'}), BashTool(), context())

    def test_capabilities_and_authority_required(self):
        config = SafetyConfig()
        action = normalize_action(execution(), WriteTool(), context())
        capabilities = {'profile_id': 'shadow', 'file_adapter': True, 'scope': 'private'}
        for auth, caps in ((None, capabilities), (authority(), {}), (authority(), {'profile_id': 'live'})):
            self.assertEqual(evaluate_policy(action, auth, config, caps, {}).decision, PolicyDecision.DENY)
        self.assertEqual(evaluate_policy(action, authority(), config, capabilities, {}).decision, PolicyDecision.ALLOW)
        missing = normalize_action(execution(), WriteTool(), {})
        self.assertEqual(evaluate_policy(missing, authority(), config, capabilities, {}).decision, PolicyDecision.DENY)
        opaque = normalize_action(execution('bash', {'command': 'python main.py'}), BashTool(), context())
        caps = {'profile_id': 'shadow', 'opaque_ro': True, 'scope': 'private'}
        self.assertEqual(evaluate_policy(opaque, authority(), config, caps, {}).decision, PolicyDecision.REVIEW)
        self.assertEqual(evaluate_policy(opaque, authority(), config, capabilities, {}).decision, PolicyDecision.DENY)
        self.assertEqual(evaluate_policy(opaque, authority(), config, caps | {'opaque_rw': True}, {}).decision, PolicyDecision.DENY)

    def test_protected_control_content(self):
        caps = {'profile_id': 'shadow', 'file_adapter': True, 'scope': 'private'}
        action = normalize_action(execution(arguments={'path': 'Makefile', 'content': 'new'}), WriteTool(), context())
        result = evaluate_policy(action, authority(), SafetyConfig(), caps, {})
        self.assertTrue(result.mandatory_human_approval)
        self.assertEqual(action.before_digest, 'b' * 64)
        self.assertTrue(action.content_digest)
        trusted = normalize_action(execution(arguments={'path': 'controller', 'content': 'x'}), WriteTool(), context())
        self.assertEqual(evaluate_policy(trusted, authority(), SafetyConfig(), caps, {}).decision, PolicyDecision.DENY)
        excluded = SafetyConfig(excluded_paths=('src',))
        ordinary = normalize_action(execution(), WriteTool(), context())
        self.assertEqual(evaluate_policy(ordinary, authority(), excluded, caps, {}).decision, PolicyDecision.DENY)
