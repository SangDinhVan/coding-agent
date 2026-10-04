import tempfile
import unittest
from pathlib import Path

from runtime.models import PolicyDecision
from runtime.safety import (
    ApprovalMode, InteractionMode, PolicyResult, ReviewVerdict, SafetyConfig,
    load_safety_config, route_approval,
)


class SafetyRoutingTests(unittest.TestCase):
    def test_config_symlink_parent_denied(self):
        from runtime.safety import _read_config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'real').mkdir()
            (root / 'real' / 'policy.json').write_text('{}')
            (root / 'alias').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaises(ValueError):
                _read_config(root / 'alias' / 'policy.json', set())

    def test_matrix(self):
        for mode in ApprovalMode:
            for interaction in InteractionMode:
                config = SafetyConfig(approval_mode=mode, interaction_mode=interaction)
                for decision in PolicyDecision:
                    for mandatory in (False, True):
                        for verdict in (None, ReviewVerdict('allow', 'reviewer_allow'),
                                        ReviewVerdict('ask', 'reviewer_ask'), ReviewVerdict('deny', 'reviewer_deny')):
                            expected = 'execute'
                            if decision == PolicyDecision.DENY:
                                expected = 'deny'
                            elif mandatory or decision == PolicyDecision.ASK or mode == ApprovalMode.ASK_ALL:
                                expected = 'human'
                            elif decision == PolicyDecision.REVIEW:
                                if mode != ApprovalMode.AUTO_REVIEW:
                                    expected = 'human'
                                elif verdict is None:
                                    expected = 'reviewer'
                                else:
                                    expected = {'allow': 'execute', 'ask': 'human', 'deny': 'deny'}[verdict.decision]
                            if expected == 'human' and interaction == InteractionMode.HEADLESS:
                                expected = 'deny'
                            with self.subTest(mode=mode, interaction=interaction, decision=decision, mandatory=mandatory, verdict=verdict):
                                self.assertEqual(route_approval(PolicyResult(decision, 'test', mandatory), config, verdict), expected)

    def test_disallowed_mode_and_invalid_configuration(self):
        for kwargs, reason in (
            ({'approval_mode': 'bad'}, 'invalid_approval_configuration'),
            ({'policy_revision': ''}, 'invalid_approval_configuration'),
            ({'allowed_modes': ()}, 'approval_mode_not_permitted'),
            ({'pinned_mode': ApprovalMode.ASK_ALL}, 'approval_mode_not_permitted'),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, reason):
                SafetyConfig(**kwargs)

    def test_restrictions_are_monotonic(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / 'repo'
            repo.mkdir()
            policy = repo / '.agent-safety.json'
            policy.write_text('{"excluded_paths": ["private"], "human_gate_paths": ["Makefile"]}')
            first = load_safety_config(None, policy, None, 'interactive', {})
            policy.unlink()
            second = load_safety_config(None, policy, None, 'interactive', first.retained_constraints)
            self.assertEqual(first.constraints_digest, second.constraints_digest)
            for body in ('{"default_mode":"auto_review"}', '{"excluded_paths":[],"excluded_paths":[]}', ' ' * 65537):
                policy.write_text(body)
                with self.assertRaisesRegex(ValueError, 'invalid_approval_configuration'):
                    load_safety_config(None, policy, None, 'interactive', {})
            policy.unlink()
            policy.symlink_to(Path(folder) / 'missing')
            with self.assertRaisesRegex(ValueError, 'invalid_approval_configuration'):
                load_safety_config(None, policy, None, 'interactive', {})
            with self.assertRaisesRegex(ValueError, 'invalid_approval_configuration'):
                load_safety_config(repo / 'host.json', policy, None, 'interactive', {})
            with self.assertRaisesRegex(ValueError, 'invalid_approval_configuration'):
                load_safety_config(Path(folder) / 'missing-host.json', policy, None, 'interactive', {})
