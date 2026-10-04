import unittest

from runtime.models import PolicyDecision
from runtime.safety import SafetyConfig, evaluate_policy, normalize_action
from tests.test_safety_policy import authority, context, execution
from tools.terminal import BashTool


class ShellPolicyTests(unittest.TestCase):
    def action(self, command, cwd='.'):
        return normalize_action(execution('bash', {'command': command, 'cwd': cwd}), BashTool(), context())

    def policy(self, action, **kwargs):
        return evaluate_policy(action, authority(), SafetyConfig(**kwargs),
                               {'profile_id': 'shadow', 'scope': 'private', 'opaque_ro': True}, {})

    def test_static_readonly_commands_have_bounded_facts_and_allow(self):
        for command in ('pwd', 'ls -la', 'find . -maxdepth 2 -type f',
                        "find . -name '*.html'", 'grep -n title index.html', 'cat index.html',
                        "grep -E 'title|body' index.html", 'head -n 20 index.html'):
            with self.subTest(command=command):
                action = self.action(command)
                self.assertFalse(action.opaque)
                self.assertEqual(action.shell_facts['kind'], 'static_readonly')
                self.assertEqual(self.policy(action).decision, PolicyDecision.ALLOW)
        action = self.action('cat index.html', 'web')
        self.assertEqual(action.shell_facts['input_paths'], ['web/index.html'])

    def test_expansion_programs_and_effectful_options_do_not_bypass_review(self):
        for command in ('ls; touch marker', 'cat $(pwd)/index.html', 'cat "$INPUT"',
                        'cat *.html', 'find . -exec sh -c true ;', 'find . -delete',
                        'find . -fprint output.txt', './ls', 'ls > output.txt',
                        "python -c 'print(1)'", 'grep -f rules.txt index.html'):
            with self.subTest(command=command):
                action = self.action(command)
                self.assertTrue(action.opaque)
                self.assertEqual(self.policy(action).decision, PolicyDecision.REVIEW)

    def test_known_test_commands_have_facts_but_still_review_project_code(self):
        for command in ('python -m unittest', 'python3 -m unittest discover -s tests',
                        'pytest tests/test_main.py -q', 'npm test'):
            with self.subTest(command=command):
                action = self.action(command)
                self.assertFalse(action.opaque)
                self.assertEqual(action.shell_facts['kind'], 'project_execution')
                self.assertEqual(self.policy(action).decision, PolicyDecision.REVIEW)

    def test_static_inputs_stay_in_scope_and_protected_paths_are_denied(self):
        for command in ('cat /etc/passwd', 'cat ../outside', 'cat .env', 'cat .git/config', 'cat controller'):
            with self.subTest(command=command):
                action = self.action(command)
                self.assertEqual(self.policy(action).decision, PolicyDecision.DENY)
        action = self.action('cat private/data.txt')
        self.assertEqual(self.policy(action, excluded_paths=('private',)).decision, PolicyDecision.DENY)
