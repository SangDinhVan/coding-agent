#!/usr/bin/env python3
"""Prepare local Docker configuration once, without installing Python packages."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def docker(*arguments):
    return subprocess.run(['docker', *arguments], check=True, capture_output=True,
                          text=True, timeout=20).stdout.strip()


def find_daemon():
    current = docker('context', 'show')
    contexts = docker('context', 'ls', '--format', '{{.Name}}').splitlines()
    saw_rootless = False
    for context in dict.fromkeys([current, *contexts]):
        try:
            endpoint = docker('context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}')
            if not endpoint.startswith('unix://'):
                continue
            socket_path = Path(endpoint.removeprefix('unix://'))
            if not socket_path.is_absolute() or not socket_path.is_socket():
                continue
            info = json.loads(docker('--context', context, 'info', '--format', '{{json .}}'))
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
        if not any('rootless' in str(item).lower() for item in info.get('SecurityOptions', [])):
            continue
        saw_rootless = True
        if str(info.get('CgroupVersion')) == '2':
            return current, context, socket_path
    if saw_rootless:
        raise RuntimeError('The safety profile requires cgroups v2. Enable it and rerun setup.')
    raise RuntimeError('No local rootless Docker daemon is available. Install/start rootless Docker, '
                       'then rerun setup: https://docs.docker.com/engine/security/rootless/')


def save_config(socket_path, image_id):
    path = ROOT / '.env'
    if path.is_symlink():
        raise RuntimeError('Refusing to replace a symlinked .env file.')
    content = (path if path.exists() else ROOT / '.env.example').read_text()
    values = {'DOCKER_SOCKET': "'" + str(socket_path).replace("'", "\\'") + "'",
              'SANDBOX_IMAGE': image_id}
    for name, value in values.items():
        pattern = re.compile(r'^(?:export\s+)?' + name + r'\s*=.*$', re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(lambda match: name + '=' + value, content)
        else:
            content = content.rstrip('\n') + '\n' + name + '=' + value + '\n'
    # Write credentials privately and replace only after the complete file is ready.
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, prefix='.env.setup-', delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def main():
    if sys.platform != 'linux':
        raise RuntimeError('This safety profile supports Linux rootless Docker only.')
    if shutil.which('docker') is None:
        raise RuntimeError('Docker is not installed. Install Docker Engine with rootless support first.')
    docker('compose', 'version')
    current, context, socket_path = find_daemon()
    print(f'Using rootless Docker context: {context}', flush=True)
    print('Building sandbox from the trusted checkout...', flush=True)
    with tempfile.TemporaryDirectory(prefix='coding-agent-setup-') as directory:
        image_file = Path(directory) / 'image.id'
        subprocess.run(['docker', '--context', context, 'build', '--iidfile', str(image_file),
                        '--tag', 'sang-coding-agent-sandbox:local', str(ROOT / 'sandbox-image')], check=True)
        image_id = image_file.read_text().strip()
    if re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) is None:
        raise RuntimeError('Docker build did not return a valid immutable image ID.')
    if context != current:
        docker('context', 'use', context)
    save_config(socket_path, image_id)
    print('Setup complete. Docker settings are saved in .env; API settings are preserved.')
    print('Set API_KEY, MODEL and BASE_URL in .env if this is your first run.')
    print('Run: docker compose run --build --rm coding-agent --approval-mode ask_on_escalation')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'Setup failed: {error}', file=sys.stderr)
        sys.exit(1)
