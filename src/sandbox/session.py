from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import shutil
import threading
import time
import tempfile
from dataclasses import asdict
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import build_changeset
from sandbox.models import ManifestEntry, ResourceLimits, SandboxPath, SandboxStatus, WorkspaceMode
from sandbox.workspace import prepare_live_workspace, prepare_workspace
from sandbox.generations import Generation, copy_tree, fsync_directory, manifest_data, seal_generation, verify_generation
from runtime.safety import AuthorityRecord, canonical_path, digest_json


class SessionError(RuntimeError):
    pass


def _tree_size(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


class SandboxSession:
    def __init__(
        self,
        session_id,
        source_workspace,
        paths: ControlPaths,
        backend,
        limits: ResourceLimits,
        *,
        mode: WorkspaceMode = WorkspaceMode.SHADOW,
        free_space_floor_bytes=5 * 1024**3,
        excluded_paths=(),
        publish_files=False,
    ):
        self.session_id = session_id
        self.source_workspace = Path(source_workspace).resolve(strict=True)
        self.paths = paths
        self.backend = backend
        self.limits = limits
        self.mode = WorkspaceMode(mode)
        self.legacy_workspace_metadata = False
        self.masks: tuple[tuple[ManifestEntry, Path], ...] = ()
        self.free_space_floor_bytes = free_space_floor_bytes
        self.status = SandboxStatus.STOPPED if paths.metadata.exists() else None
        self.baseline_bytes = 0
        self.isolation_level = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self.excluded_paths = tuple(excluded_paths)
        self.before_generation = None
        self.current_generation = None
        self.authority = None
        self._file_adapter_evidence = {}
        self._file_grant = None
        self._exec_grant = None
        self.publish_files = publish_files
        self._publication = None
        self.action_quiescent = True
        self._helper = None
        self._helper_digest = ''
        self.quarantined = False
        if paths.metadata.exists():
            data = json.loads(paths.metadata.read_text())
            # Legacy sessions retain their original private-only effect scope.
            self.publish_files = bool(data.get('publish_files', False))
            self.status = SandboxStatus(data["status"])
            self.legacy_workspace_metadata = "workspace_mode" not in data
            self.mode = WorkspaceMode(data.get("workspace_mode", WorkspaceMode.SHADOW.value))
            self.baseline_bytes = int(data.get("baseline_bytes", 0))
            self.isolation_level = data.get("isolation_level")
            self.masks = tuple(
                (
                    ManifestEntry(path=SandboxPath(item["path"]), kind=item["kind"], reason=item.get("reason")),
                    self.paths.mask_directory if item["kind"] == "directory" else self.paths.mask_file,
                )
                for item in data.get("masks", [])
            )
            self.backend.mode = self.mode
            self.backend.mount_source = self.mount_source
            self.backend.masks = self.masks
            self.excluded_paths = tuple(data.get('excluded_paths', self.excluded_paths))
            self.quarantined = data.get('quarantined', False)
            if data.get('authority'):
                auth = dict(data['authority'])
                auth['allowed_operations'] = tuple(auth['allowed_operations'])
                auth['allowed_sinks'] = tuple(auth['allowed_sinks'])
                self.authority = AuthorityRecord(**auth)
            self.before_generation = self._restore_generation(data.get('before_generation'))
            self.current_generation = self._restore_generation(data.get('current_generation'))
        if self.mode != WorkspaceMode.SHADOW:
            raise SessionError('unsupported_profile')

    def _restore_generation(self, record):
        if not record:
            return None
        digest = record.get('digest', '')
        if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
            raise SessionError('invalid_generation')
        entries = tuple(ManifestEntry(path=SandboxPath(item['path']), kind=item['kind'],
                                      mode=item.get('mode'), size=item.get('size'), sha256=item.get('sha256'))
                        for item in record['manifest'])
        result = Generation(digest, self.paths.generations / digest, entries)
        if not verify_generation(result):
            raise SessionError('invalid_generation')
        return result

    def _trusted_file_helper(self):
        if self._helper is None:
            from sandbox.helpers import helper_path
            path = helper_path('sandbox_fs')
            self._helper_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            spec = importlib.util.spec_from_file_location('trusted_sandbox_fs', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._helper = module
        return self._helper

    def _seal_current(self):
        self.current_generation = None
        result = seal_generation(self.paths.workspace, self.paths.generations, max_bytes=100 * 1024**2, max_entries=10000)
        self.current_generation = result
        return result

    def _persist(self, **extra):
        payload = {
            "managed": True, "session_id": self.session_id, "status": self.status.value,
            "container_id": self.backend.container_id, "workspace_identity": self.paths.workspace_identity,
            "image_digest": getattr(self.backend, "image", "test"), "limits": asdict(self.limits),
            "baseline_bytes": self.baseline_bytes, "workspace_mode": self.mode.value,
            "isolation_level": self.isolation_level,
            "masks": [
                {"path": str(entry.path), "kind": entry.kind, "reason": entry.reason}
                for entry, _ in self.masks
            ],
            "updated_at": time.time(), **extra,
            'excluded_paths': list(self.excluded_paths),
            'quarantined': self.quarantined,
            'publish_files': self.publish_files,
            'authority': asdict(self.authority) if self.authority else None,
            'before_generation': {'digest': self.before_generation.digest, 'manifest': manifest_data(self.before_generation.manifest)} if self.before_generation else None,
            'current_generation': {'digest': self.current_generation.digest, 'manifest': manifest_data(self.current_generation.manifest)} if self.current_generation else None,
        }
        temporary = self.paths.metadata.with_suffix(".tmp")
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, self.paths.metadata)
        fsync_directory(self.paths.session_dir)
        self.paths.heartbeat.touch(mode=0o600)

    def _daemon_source(self, path: Path) -> Path:
        mapper = getattr(self.backend, "daemon_source", None)
        return mapper(path) if mapper else Path(path).resolve(strict=True)

    @property
    def mount_source(self) -> Path:
        return self.source_workspace if self.mode == WorkspaceMode.LIVE else self.paths.workspace

    def create(self):
        if self.status is not None:
            raise SessionError("session already exists")
        evidence = self.backend.preflight(self.limits)
        self.isolation_level = evidence.get("isolation_level")
        if self.mode == WorkspaceMode.LIVE:
            manifest = prepare_live_workspace(
                self.source_workspace, self.paths,
                free_space_floor_bytes=self.free_space_floor_bytes,
            )
            self.masks = tuple(
                (
                    entry,
                    self.paths.mask_directory if entry.kind == "directory" else self.paths.mask_file,
                )
                for entry in manifest.excluded
            )
        else:
            manifest = prepare_workspace(
                self.source_workspace, self.paths,
                free_space_floor_bytes=self.free_space_floor_bytes,
                baseline_limit_bytes=100 * 1024**2, excluded_paths=self.excluded_paths,
            )
        self.excluded_paths = tuple(sorted(set(self.excluded_paths) | {str(item.path) for item in manifest.excluded}))
        self.before_generation = self._seal_current()
        self.authority = AuthorityRecord(
            digest_json({'session': self.session_id, 'source': self.before_generation.digest}),
            self.session_id, self.before_generation.digest, self.paths.workspace_identity,
            ('read', 'write', 'edit', 'bash'), ('main_model', 'reviewer', 'summarizer', 'memory', 'ui', 'audit_text', 'trace'),
        )
        self.baseline_bytes = manifest.total_bytes
        self.backend.create(
            self.session_id, self.paths.workspace_identity, self.mount_source, self.limits,
            mode=self.mode, masks=self.masks,
        )
        self.status = SandboxStatus.CREATED
        self._persist()

    def start(self):
        if self.status not in {SandboxStatus.CREATED, SandboxStatus.STOPPED}:
            raise SessionError("sandbox cannot start from current state")
        self.backend.start()
        try:
            self.backend.verify_runtime(self.limits, self.mount_source)
        except BaseException as error:
            try:
                self.backend.stop()
                if self.backend.inspect().get("running"):
                    raise SessionError("sandbox is still running after runtime probe failure")
            finally:
                self.status = SandboxStatus.ERROR
                self._persist(error=f"runtime_probe: {error}")
            raise
        self.status = SandboxStatus.RUNNING
        verifier = getattr(self.backend, 'verify_file_adapter', None)
        if verifier is not None:
            try:
                self._trusted_file_helper()
                self._file_adapter_evidence = verifier(self.limits, self.paths.workspace, self._helper_digest)
            except Exception:
                self._file_adapter_evidence = {}
        self._persist()

    def security_context(self):
        if self.status != SandboxStatus.RUNNING or self.quarantined or self.authority is None or self._file_adapter_evidence.get('file_adapter') is not True or self._file_adapter_evidence.get('rootless') is not True or self._file_adapter_evidence.get('helper_digest') != self._helper_digest:
            return {}
        if not verify_generation(self.before_generation):
            raise SessionError('invalid_generation')
        generation = self._seal_current()
        self._persist()
        states = {str(item.path): {key: value for key, value in data.items() if key != 'path'}
                  for item, data in zip(generation.manifest, manifest_data(generation.manifest))}
        controls = {'Makefile', 'makefile', 'GNUmakefile', 'package.json', 'pyproject.toml', 'setup.py',
                    'setup.cfg', 'tox.ini', 'conftest.py', '.agentignore', '.agent-safety.json',
                    '.vscode/tasks.json', '.vscode/settings.json', 'compose.yaml', 'Dockerfile'}
        roles = {path: 'project_control' for path in controls}
        for path in states:
            if path.startswith('.github/') or path.endswith('/conftest.py'):
                roles[path] = 'project_control'
        from sandbox.workspace import _HARD_PATTERNS, _matches
        def classify(path):
            if path and (_matches(path, _HARD_PATTERNS) or any(path == item or path.startswith(item + '/') for item in self.excluded_paths)):
                return 'excluded'
            if path and (path.startswith('.github/') or path.endswith('/conftest.py')):
                return 'project_control'
            return roles.get(path, 'ordinary')
        return {
            'authority': self.authority, 'generation_digest': generation.digest,
            'scope': self.paths.workspace_identity, 'before_states': states, 'path_roles': roles,
            'classify_path': classify,
            'image_digest': self.backend.image,
            'environment_digest': digest_json({'helper_digest': self._helper_digest, 'limits': asdict(self.limits)}),
            'capabilities': {'profile_id': 'shadow', 'scope': self.paths.workspace_identity,
                             'file_adapter': True, 'opaque_ro': bool(self._file_adapter_evidence.get('opaque_ro')),
                             'publish_files': self.publish_files},
            'checkpoint_action': self.checkpoint_action,
            'opaque_input_preview': self.opaque_input_preview,
            'shell_input_preview': self.shell_input_preview,
        }

    def restrict_paths(self, paths):
        retained = tuple(sorted(set(self.excluded_paths) | set(paths)))
        if retained != self.excluded_paths:
            self.excluded_paths = retained
            self._persist()

    def _excluded_reason(self, path):
        return 'retained_exclusion' if any(path == root or path.startswith(root + '/') for root in self.excluded_paths) else None

    def checkpoint_action(self, action):
        if self.current_generation is None or self.current_generation.digest != action.generation_digest or not verify_generation(self.current_generation):
            raise SessionError('invalid_generation')
        shell = action.tool_name == 'bash'
        grant = ({'shell_facts': action.shell_facts} if shell else self._trusted_file_helper().make_grant(
            {'operation': action.tool_name, **json.loads(action.arguments_json)}, self.paths.workspace,
            protected=action.path_role == 'project_control',
        ))
        if not shell and grant['before_digest'] != action.before_digest:
            raise SessionError('before_state_changed')
        if self.publish_files and action.tool_name in {'write', 'edit'}:
            from sandbox.publication import prepare_publication
            grant['publication'] = prepare_publication(self.source_workspace, self.paths.workspace_identity,
                                                       action.path, action.before_digest, self._trusted_file_helper())
        payload = {'execution_id': action.execution_id, 'generation_digest': action.generation_digest,
                   'argument_digest': action.argument_digest, 'before_digest': action.before_digest,
                   'grant': grant, 'before_directory': str(self.current_generation.directory)}
        digest = digest_json(payload)
        self.paths.checkpoints.mkdir(mode=0o700, exist_ok=True)
        destination = self.paths.checkpoints / (digest + '.json')
        temporary = destination.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        fsync_directory(self.paths.checkpoints)
        fsync_directory(self.paths.session_dir)
        return digest

    def authorize_exec_action(self, action, binding):
        if self.quarantined or not self.action_quiescent or binding.generation_digest != action.generation_digest or self.current_generation is None or self.current_generation.digest != action.generation_digest or not verify_generation(self.current_generation):
            raise SessionError('exec_binding_changed')
        self._exec_grant = (action, binding, self.current_generation)

    def approval_preview(self, execution):
        if execution.tool_name == 'update_plan':
            return {'scope': 'internal_plan_state'}
        if execution.tool_name not in {'bash', 'read', 'write', 'edit'}:
            raise SessionError('unsupported_approval_preview')
        record = execution.authorization.get('binding', {})
        digest = record.get('generation_digest')
        if not digest or self.current_generation is None or self.current_generation.digest != digest or not verify_generation(self.current_generation):
            raise SessionError('approval_content_unavailable')
        path = execution.arguments.get('path')
        if execution.tool_name == 'bash':
            from runtime.shell import analyze_shell
            facts = analyze_shell(execution.arguments['command'], execution.arguments.get('cwd', '.'))
            if facts['kind'] == 'static_readonly':
                return {'scope': record.get('scope_digest'), 'generation_digest': digest, 'shell_facts': facts}
            return {'scope': record.get('scope_digest'), 'generation_digest': digest,
                    'sealed_inputs': self.opaque_input_preview(digest)}
        if path is None:
            raise SessionError('approval_content_unavailable')
        helper = self._trusted_file_helper()
        before = helper.target_state(self.current_generation.directory, path)
        content = '' if before['kind'] == 'absent' else helper._read(self.current_generation.directory, path).decode('utf-8')
        before['content'] = content
        after = execution.arguments.get('content')
        if execution.tool_name == 'edit':
            old = execution.arguments['old_string']
            if not old or content.count(old) != 1:
                raise SessionError('approval_content_unavailable')
            after = content.replace(old, execution.arguments['new_string'], 1)
        return {'path': path, 'before': before, 'after_content': after,
                'destination': str(self.source_workspace / path) if self.publish_files else 'private workspace'}

    def shell_input_preview(self, action):
        return self.opaque_input_preview(action.generation_digest, paths=action.shell_facts['input_paths'])

    def opaque_input_preview(self, digest, *, paths=None):
        generation = self.current_generation
        if generation is None or generation.digest != digest or not verify_generation(generation):
            raise SessionError('approval_content_unavailable')
        contents, budget = {}, 0
        for item in generation.manifest:
            if item.kind != 'file' or self._excluded_reason(str(item.path)):
                continue
            if paths is not None and not any(path == '.' or str(item.path) == path
                                            or str(item.path).startswith(path + '/') for path in paths):
                continue
            budget += item.size
            if budget > 2 * 1024**2:
                raise SessionError('approval_content_budget')
            try:
                contents[str(item.path)] = (generation.directory / item.path).read_text(encoding='utf-8')
            except UnicodeError:
                raise SessionError('approval_content_encoding') from None
        return contents

    def authorize_file_action(self, action, binding):
        if binding.generation_digest != action.generation_digest or self.current_generation is None or self.current_generation.digest != action.generation_digest:
            raise SessionError('file grant binding changed')
        grant = self._trusted_file_helper().make_grant(
            {'operation': action.tool_name, **json.loads(action.arguments_json)}, self.paths.workspace,
            protected=action.path_role == 'project_control',
        )
        if grant['before_digest'] != binding.before_digest:
            raise SessionError('file grant before-state changed')
        self._publication = None
        if self.publish_files and action.tool_name in {'write', 'edit'}:
            from sandbox.publication import prepare_publication
            helper = self._trusted_file_helper()
            publication = prepare_publication(self.source_workspace, self.paths.workspace_identity,
                                               action.path, action.before_digest, helper)
            arguments = json.loads(action.arguments_json)
            content = arguments.get('content')
            if action.tool_name == 'edit':
                content = helper._read(self.paths.workspace, action.path).decode('utf-8')
                old = arguments['old_string']
                if not old or content.count(old) != 1:
                    raise SessionError('invalid_edit')
                content = content.replace(old, arguments['new_string'], 1)
            from runtime.safety import screen_artifact
            from core.config import API_KEY
            if screen_artifact(content, action.generation_digest, (API_KEY,))['secret_exposure_detected']:
                raise SessionError('publication_disclosure_denied')
            self._publication = publication, content.encode('utf-8')
        self._file_grant = grant

    def start_watchdog(self, *, interval_seconds=5.0):
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()

        def watch():
            while not self._watchdog_stop.wait(interval_seconds):
                self.paths.heartbeat.touch(mode=0o600)
                if self.status == SandboxStatus.RUNNING and not self.check_disk_budget():
                    return

        self._watchdog_thread = threading.Thread(
            target=watch,
            name=f"sandbox-watchdog-{self.session_id}",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop_watchdog(self):
        self._watchdog_stop.set()
        if self._watchdog_thread is not None and self._watchdog_thread is not threading.current_thread():
            self._watchdog_thread.join(timeout=2)

    def stop(self, reason="requested"):
        self.stop_watchdog()
        if self.status == SandboxStatus.RUNNING:
            self.backend.stop()
        elif self.status not in {SandboxStatus.CREATED, SandboxStatus.STOPPED, SandboxStatus.ERROR}:
            raise SessionError("sandbox cannot stop from current state")
        self.status = SandboxStatus.STOPPED
        self._persist(stop_reason=reason)

    def resume(self):
        if self.status != SandboxStatus.STOPPED:
            raise SessionError("only a stopped sandbox can resume")
        metadata = json.loads(self.paths.metadata.read_text())
        if self.quarantined:
            raise SessionError('recovery_required')
        self.backend.preflight(self.limits)
        inspected = self.backend.inspect()
        container_id = metadata.get("container_id")
        if inspected.get("container_id") != container_id or self.backend.container_id != container_id:
            raise SessionError("container identity mismatch")
        expected_labels = {
            "io.sang-coding-agent.managed": "true",
            "io.sang-coding-agent.session": self.session_id,
            "io.sang-coding-agent.workspace": self.paths.workspace_identity,
            "io.sang-coding-agent.image": metadata.get("image_digest"),
        }
        if not self.legacy_workspace_metadata:
            expected_labels["io.sang-coding-agent.workspace-mode"] = self.mode.value
        expected = {
            "image_digest": metadata.get("image_digest"),
            "user": "0:0" if self.mode == WorkspaceMode.LIVE else "65532:65532",
            "mount_source": str(self._daemon_source(self.mount_source)),
            "mount_rw": True,
            "network_mode": "none",
            "read_only_rootfs": True,
            "pids": self.limits.pids,
            "memory_bytes": self.limits.memory_bytes,
            "memory_swap_bytes": self.limits.memory_bytes,
            "cpus": self.limits.cpus,
        }
        mismatch = any(inspected.get(key) != value for key, value in expected.items())
        mismatch = mismatch or any(inspected.get("labels", {}).get(key) != value for key, value in expected_labels.items())
        inspected_mounts = {item.get("Destination"): item for item in inspected.get("mounts", [])}
        mismatch = mismatch or any(
            inspected_mounts.get(f"/workspace/{entry.path}", {}).get("Source") != str(self._daemon_source(source))
            or inspected_mounts.get(f"/workspace/{entry.path}", {}).get("RW") is not False
            for entry, source in self.masks
        )
        mismatch = mismatch or set(inspected.get("cap_drop", [])) != {"ALL"}
        mismatch = mismatch or "no-new-privileges:true" not in inspected.get("security_opt", [])
        if mismatch:
            raise SessionError("sandbox reconciliation mismatch")
        self.start()

    def prepare_changes(self):
        if self.quarantined:
            raise SessionError('recovery_required')
        if self.mode == WorkspaceMode.LIVE:
            raise SessionError("live workspace changes are already applied")
        if self.status == SandboxStatus.RUNNING:
            self.stop("prepare_changes")
        if self.status != SandboxStatus.STOPPED:
            raise SessionError("sandbox must be stopped before sealing changes")
        if not self.action_quiescent or self.backend.inspect().get('running'):
            self.quarantined = True
            self._persist(error='quiescence_unknown')
            raise SessionError('quiescence_unknown')
        generation = self._seal_current()
        changes = build_changeset(self.paths, before_generation=self.before_generation,
                                  current_generation=generation, quiescent=True)
        self.status = SandboxStatus.SEALED
        self._persist(change_set_hash=changes.change_set_hash, approvable=changes.approvable)
        return changes

    def check_disk_budget(self, *, baseline_bytes=None, growth_limit_bytes=None, free_space_floor_bytes=None):
        baseline = self.baseline_bytes if baseline_bytes is None else baseline_bytes
        growth = self.limits.workspace_growth_bytes if growth_limit_bytes is None else growth_limit_bytes
        floor = self.free_space_floor_bytes if free_space_floor_bytes is None else free_space_floor_bytes
        exceeded = _tree_size(self.mount_source) - baseline > growth
        low_space = shutil.disk_usage(self.paths.session_dir).free < floor
        if exceeded or low_space:
            if self.status == SandboxStatus.RUNNING:
                self.backend.stop()
            self.status = SandboxStatus.ERROR
            self._persist(error="workspace_budget" if exceeded else "host_free_space")
            return False
        return True

    def destroy(self, reason="discard"):
        self.stop_watchdog()
        self.backend.destroy()
        self.status = SandboxStatus.DESTROYED
        self._persist(destroy_reason=reason)

    def exec(self, request):
        grant, self._exec_grant = self._exec_grant, None
        if grant is None or self.status != SandboxStatus.RUNNING or self.quarantined:
            raise SessionError('exec_grant_required')
        action, binding, generation = grant
        arguments = json.loads(action.arguments_json)
        command = arguments['command']
        if arguments.get('verification_kind') is not None:
            command = 'set -euo pipefail\n' + command
        if request.action_id != action.execution_id or request.command != command or str(request.cwd) != action.cwd:
            raise SessionError('exec_grant_mismatch')
        if not verify_generation(generation):
            raise SessionError('invalid_generation')
        # Each opaque action sees an independent copy, never the immutable recovery inputs.
        view = Path(tempfile.mkdtemp(prefix='action-', dir=self.paths.session_dir))
        self.action_quiescent = False
        backend = type(self.backend)(self.backend.image, runner=self.backend.runner, control_container_id=self.backend.control_container_id)
        try:
            entries, _, _ = copy_tree(generation.directory, view, max_bytes=100 * 1024**2, max_entries=10000,
                                      exclude=self._excluded_reason)
            view.chmod(0o755)
            backend.preflight(self.limits)
            result = backend.exec_action(request, Generation(generation.digest, view, entries), self.limits)
            if not result.quiescent:
                raise SessionError('action_quiescence_unknown')
            self.action_quiescent = True
            return result
        except BaseException:
            self.quarantined = True
            self.status = SandboxStatus.ERROR
            self._persist(error='opaque_outcome_unknown')
            raise
        finally:
            if self.action_quiescent:
                shutil.rmtree(view)

    def fs_call(self, request):
        if self.status != SandboxStatus.RUNNING:
            raise SessionError("sandbox is not running")
        grant, self._file_grant = self._file_grant, None
        if grant is None or not self._file_adapter_evidence.get('file_adapter'):
            raise SessionError('file grant required')
        publication, self._publication = self._publication, None
        try:
            from sandbox.changes import _workspace_lock
            with _workspace_lock(self.paths):
                if publication is not None:
                    from sandbox.publication import prepare_publication, publish_file
                    expected, data = publication
                    fresh = prepare_publication(self.source_workspace, self.paths.workspace_identity,
                                                expected['path'], expected['before_digest'], self._trusted_file_helper())
                    if fresh != expected:
                        raise SessionError('source_parent_changed')
                result = self.backend.fs_call(request, grant=grant)
                if publication is not None and result.get('success'):
                    if result.get('sha256') != hashlib.sha256(data).hexdigest():
                        raise SessionError('publication_content_changed')
                    publish_file(self.source_workspace, self.paths.workspace_identity, expected, data, self._trusted_file_helper())
                    result['published_path'] = str(self.source_workspace / expected['path'])
        except BaseException:
            self.status = SandboxStatus.ERROR
            self.quarantined = True
            self.current_generation = None
            self._persist(error='file_adapter_unknown')
            raise
        if request.get('operation') in {'write', 'edit'}:
            self.current_generation = None
            if not result.get('success'):
                self.quarantined = True
                self.status = SandboxStatus.ERROR
                self._persist(error='file_adapter_unknown')
        return result

    def fingerprint(self, relative_path):
        path = canonical_path(str(relative_path))
        if self.status != SandboxStatus.RUNNING or self.authority is None:
            return None
        try:
            state = self._trusted_file_helper().target_state(self.paths.workspace, path)
            return state.get('sha256')
        except (OSError, ValueError, RuntimeError):
            return None

    def recovery_state_fingerprint(self, path):
        if self.status != SandboxStatus.RUNNING or self.quarantined or not self.action_quiescent:
            return None
        try:
            helper = self._trusted_file_helper()
            state = helper.target_state(self.paths.workspace, canonical_path(path))
            if self.publish_files:
                from sandbox.publication import open_source
                fd = open_source(self.source_workspace, self.paths.workspace_identity)
                try:
                    return digest_json({'shadow': state, 'source': helper.target_state(f'/proc/self/fd/{fd}/.', path)})
                finally:
                    os.close(fd)
            return digest_json(state)
        except (OSError, ValueError, RuntimeError):
            return None

    def recovery_states(self, operation, arguments):
        helper = self._trusted_file_helper()
        path = arguments['path']
        before = helper.target_state(self.paths.workspace, path)
        if operation == 'write':
            content = arguments['content']
        else:
            current = helper._read(self.paths.workspace, path).decode('utf-8')
            old = arguments['old_string']
            if not old or current.count(old) != 1:
                raise SessionError('invalid_recovery_before')
            content = current.replace(old, arguments['new_string'], 1)
        data = content.encode()
        after = {'kind': 'file', 'mode': before.get('mode', 0o644), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        if self.publish_files:
            return {'before_state_digest': digest_json({'shadow': before, 'source': before}),
                    'expected_after_state_digest': digest_json({'shadow': after, 'source': after})}
        return {'before_state_digest': digest_json(before), 'expected_after_state_digest': digest_json(after)}


def cleanup_expired(root: Path, *, now=None, ttl_seconds=24 * 3600, backend_factory):
    now = time.time() if now is None else now
    removed = []
    sandboxes = root / "sandboxes"
    if not sandboxes.exists():
        return removed
    for session_dir in sandboxes.iterdir():
        metadata_path = session_dir / "metadata.json"
        try:
            data = json.loads(metadata_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if data.get("managed") is not True or data.get("session_id") != session_dir.name:
            continue
        if now - float(data.get("updated_at", now)) <= ttl_seconds:
            continue
        backend = backend_factory(data)
        backend.destroy()
        shutil.rmtree(session_dir)
        removed.append(session_dir.name)
    return removed
