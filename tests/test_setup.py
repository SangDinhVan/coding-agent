import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / 'scripts' / 'setup.py'

FAKE_DOCKER = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['SETUP_TEST_ROOT'])
scenario = os.environ.get('SETUP_TEST_SCENARIO', '')
with (root / 'docker-calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\\n')
context = 'default'
if args[:1] == ['--context']:
    context, args = args[1], args[2:]
if args[:2] == ['compose', 'version']:
    print('Docker Compose version v2.40.0')
elif args == ['context', 'show']:
    print('default')
elif args[:2] == ['context', 'ls']:
    print('default\\nteam-engine')
elif args[:2] == ['context', 'inspect']:
    print('unix://' + str(root / 'docker.sock'))
elif args[:1] == ['info']:
    rootless = context == 'team-engine' and scenario != 'rootful'
    print(json.dumps({'SecurityOptions': ['name=rootless'] if rootless else [],
                      'CgroupVersion': '1' if scenario == 'cgroup1' else '2'}))
elif args[:1] == ['build']:
    if scenario == 'build-fail':
        sys.exit(1)
    image_file = pathlib.Path(args[args.index('--iidfile') + 1])
    image_file.write_text('sha256:' + 'a' * 64)
elif args[:2] == ['context', 'use']:
    (root / 'selected-context').write_text(args[2])
else:
    sys.exit('Unexpected Docker command: ' + repr(args))
'''


class SetupTests(unittest.TestCase):
    def run_setup(self, root, scenario='', existing_env=None):
        (root / 'scripts').mkdir()
        if SETUP.exists():
            (root / 'scripts' / 'setup.py').write_bytes(SETUP.read_bytes())
        (root / 'sandbox-image').mkdir()
        (root / '.env.example').write_text('API_KEY=your_api_key_here\nMODEL=your_model_id\nBASE_URL=...\n')
        if existing_env is not None:
            (root / '.env').write_text(existing_env)
        binary = root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(root / 'docker.sock'))
            env = os.environ | {'PATH': f'{binary}:{os.environ["PATH"]}',
                                'SETUP_TEST_ROOT': str(root), 'SETUP_TEST_SCENARIO': scenario}
            result = subprocess.run([sys.executable, str(root / 'scripts' / 'setup.py')],
                                    cwd=root, env=env, capture_output=True, text=True)
        return result

    def test_new_checkout_detects_custom_rootless_context_and_creates_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_setup(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (root / '.env').read_text()
            self.assertIn('SANDBOX_IMAGE=sha256:' + 'a' * 64, config)
            self.assertIn(str(root / 'docker.sock'), config)
            self.assertIn('API_KEY=your_api_key_here', config)
            self.assertNotIn('DOCKER_CONTEXT=', config)
            self.assertEqual((root / 'selected-context').read_text(), 'team-engine')
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)
            calls = [json.loads(line) for line in (root / 'docker-calls.jsonl').read_text().splitlines()]
            build = next(call for call in calls if 'build' in call)
            self.assertEqual(build[:3], ['--context', 'team-engine', 'build'])
            self.assertEqual(build[-1], str(root / 'sandbox-image'))

    def test_setup_preserves_provider_settings_and_replaces_stale_machine_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = '# Keep this comment\nAPI_KEY="private-test-key"\nMODEL=my-model\nBASE_URL=https://provider.invalid\n'
            result = self.run_setup(root, existing_env=provider + 'DOCKER_SOCKET=/old/socket\nSANDBOX_IMAGE=old\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (root / '.env').read_text()
            self.assertTrue(config.startswith(provider))
            self.assertEqual(config.count('DOCKER_SOCKET='), 1)
            self.assertEqual(config.count('SANDBOX_IMAGE='), 1)
            self.assertNotIn('/old/socket', config)
            self.assertNotIn('private-test-key', result.stdout + result.stderr)

    def test_unsupported_daemon_leaves_configuration_untouched(self):
        for scenario, reason in [('rootful', 'rootless Docker'), ('cgroup1', 'cgroups v2')]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = 'API_KEY=keep-me\n'
                result = self.run_setup(root, scenario, original)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)
                self.assertEqual((root / '.env').read_text(), original)
                self.assertFalse((root / 'selected-context').exists())
                self.assertNotIn('build', (root / 'docker-calls.jsonl').read_text())

    def test_failed_build_does_not_replace_config_or_switch_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = 'API_KEY=keep-me\nSANDBOX_IMAGE=previous\n'
            result = self.run_setup(root, 'build-fail', original)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / '.env').read_text(), original)
            self.assertFalse((root / 'selected-context').exists())
            self.assertIn('build', (root / 'docker-calls.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
