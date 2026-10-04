from __future__ import annotations

import json
import logging
import subprocess
import selectors
import os
import time
import hashlib
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable

from sandbox.models import ExecRequest, ExecutionStatus, ManifestEntry, ResourceLimits, SandboxResult, ViolationStatus, WorkspaceMode


logger = logging.getLogger(__name__)
IDLE_COMMAND = ['-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/tmp', '/usr/local/bin/python', '-I', '-c', 'import time; time.sleep(86400)']


class DockerIsolationLevel(str, Enum):
    ROOTLESS = "ROOTLESS"
    VM_ISOLATED = "VM_ISOLATED"
    ROOTFUL_BARE = "ROOTFUL_BARE"


def classify_isolation_level(info: dict) -> DockerIsolationLevel:
    security_options = info.get("SecurityOptions") or []
    if any("rootless" in str(option).lower() for option in security_options):
        return DockerIsolationLevel.ROOTLESS
    desktop_evidence = " ".join((
        str(info.get("OperatingSystem", "")),
        str(info.get("ServerVersion", "")),
    )).lower()
    if "docker desktop" in desktop_evidence:
        return DockerIsolationLevel.VM_ISOLATED
    return DockerIsolationLevel.ROOTFUL_BARE


class DockerPreflightError(RuntimeError):
    pass


class DockerTransportError(RuntimeError):
    pass


def _run(argv, *, input=None, timeout=None):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    selector = selectors.DefaultSelector()
    pending = memoryview((input or '').encode())
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + (timeout or 30)
    total = 0
    try:
        for stream, kind in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, kind)
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(0.05):
                if key.data == 'stdin':
                    try:
                        written = os.write(key.fd, pending[:65536])
                        pending = pending[written:]
                    except BrokenPipeError:
                        pending = memoryview(b'')
                    if not pending:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > 64 * 1024**2:
                        raise DockerTransportError('transport_output_limit')
                    chunks[key.data].extend(data)
        process.wait()
        return subprocess.CompletedProcess(argv, process.returncode, chunks['stdout'].decode('utf-8', 'replace'), chunks['stderr'].decode('utf-8', 'replace'))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _translated_source(path: Path, mounts: tuple[tuple[Path, Path], ...]) -> Path | None:
    for destination, source in sorted(mounts, key=lambda item: len(item[0].parts), reverse=True):
        try:
            relative = path.relative_to(destination)
        except ValueError:
            continue
        return source / relative
    return None


class DockerBackend:
    def __init__(self, image: str, *, runner: Callable = _run, control_container_id: str = ""):
        digest = image.rsplit("@sha256:", 1)[1] if "@sha256:" in image else image.removeprefix("sha256:")
        is_local_id = image.startswith("sha256:") and image.count(":") == 1
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) or not ("@sha256:" in image or is_local_id):
            raise DockerPreflightError("sandbox image must use an immutable sha256 digest or image ID")
        self.image = image
        self.runner = runner
        self.container_id = None
        self.creation_token = ''
        self.session_id = ""
        self.daemon_id = ""
        self.isolation_level = None
        self.control_container_id = control_container_id
        self.control_mounts: tuple[tuple[Path, Path], ...] = ()
        self.mode = WorkspaceMode.SHADOW
        self.mount_source = None
        self.masks: tuple[tuple[ManifestEntry, Path], ...] = ()
        self.action_profile = False

    def _load_control_mounts(self):
        if not self.control_container_id or self.control_mounts:
            return
        control = json.loads(self._checked(["docker", "inspect", self.control_container_id]))
        if len(control) != 1 or not str(control[0].get("Id", "")).startswith(self.control_container_id):
            raise DockerPreflightError("control container identity mismatch")
        self.control_mounts = tuple(
            (Path(item["Destination"]), Path(item["Source"]))
            for item in control[0].get("Mounts", [])
            if item.get("Destination") and item.get("Source")
        )

    def _checked(self, argv, *, input=None, timeout=30):
        try:
            result = self.runner(argv, input=input, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise DockerTransportError(f"Docker transport failed: {argv[1]}") from error
        if result.returncode:
            raise DockerTransportError(result.stderr.strip() or f"Docker command failed: {argv[1]}")
        return result.stdout

    def preflight(self, limits: ResourceLimits):
        info = json.loads(self._checked(["docker", "info", "--format", "{{json .}}"] ))
        self.isolation_level = classify_isolation_level(info)
        if self.isolation_level == DockerIsolationLevel.ROOTFUL_BARE:
            raise DockerPreflightError("Docker daemon isolation level ROOTFUL_BARE is not allowed")
        if self.isolation_level == DockerIsolationLevel.VM_ISOLATED:
            logger.warning(
                "Docker Desktop VM isolation is accepted for this project, "
                "but it is not equivalent to rootless Docker"
            )
        if str(info.get("CgroupVersion")) != "2":
            raise DockerPreflightError("cgroups v2 is required")
        self._load_control_mounts()
        inspected = json.loads(self._checked(["docker", "image", "inspect", self.image]))
        matches_image = len(inspected) == 1 and (
            inspected[0].get("Id") == self.image
            if self.image.startswith("sha256:")
            else self.image in inspected[0].get("RepoDigests", [])
        )
        if not matches_image:
            raise DockerPreflightError("configured image digest is not present locally")
        user = str(inspected[0].get("Config", {}).get("User", ""))
        if not user or user.split(":", 1)[0] in {"0", "root"}:
            raise DockerPreflightError("sandbox image must configure a non-root user")
        self.daemon_id = str(info.get("ID", ""))
        return {
            "daemon_id": self.daemon_id,
            "isolation_level": self.isolation_level.value,
            "image": self.image,
            "limits": limits,
        }

    def daemon_source(self, path: Path) -> Path:
        self._load_control_mounts()
        resolved = Path(path).resolve(strict=True)
        translated = _translated_source(resolved, self.control_mounts)
        if translated is not None:
            return translated
        if self.control_container_id:
            raise DockerPreflightError(
                f"No daemon-visible bind source for {resolved}; control container mounts do not cover this path"
            )
        return resolved

    def create(
        self,
        session_id: str,
        workspace_identity: str,
        workspace: Path,
        limits: ResourceLimits,
        *,
        mode: WorkspaceMode = WorkspaceMode.SHADOW,
        masks: tuple[tuple[ManifestEntry, Path], ...] = (),
    ) -> str:
        workspace = workspace.resolve(strict=True)
        daemon_workspace = self.daemon_source(workspace)
        mode = WorkspaceMode(mode)
        if "," in str(daemon_workspace):
            raise DockerPreflightError("Docker bind mount paths cannot contain a comma")
        runtime_user = "0:0" if mode == WorkspaceMode.LIVE else "65532:65532"
        argv = [
            "docker", "create", "--pull", "never", "--name", f"coding-agent-{session_id}",
            "--label", "io.sang-coding-agent.managed=true",
            "--label", f"io.sang-coding-agent.session={session_id}",
            "--label", f"io.sang-coding-agent.workspace={workspace_identity}",
            "--label", f"io.sang-coding-agent.image={self.image}",
            "--label", f"io.sang-coding-agent.workspace-mode={mode.value}",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", str(limits.pids),
            "--memory", str(limits.memory_bytes), "--memory-swap", str(limits.memory_bytes),
            "--cpus", str(limits.cpus),
            "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={limits.tmpfs_bytes},nr_inodes={limits.tmpfs_inodes}",
            "--mount", f"type=bind,src={daemon_workspace},dst=/workspace" + (",readonly" if self.action_profile else ""),
        ]
        normalized_masks = []
        for entry, source in masks:
            source = Path(source).resolve(strict=True)
            daemon_source = self.daemon_source(source)
            target = f"/workspace/{entry.path}"
            if "," in str(daemon_source) or "," in target:
                raise DockerPreflightError("Docker bind mount paths cannot contain a comma")
            argv.extend(("--mount", f"type=bind,src={daemon_source},dst={target},readonly"))
            normalized_masks.append((entry, source))
        argv.extend(('--entrypoint', '/usr/bin/env'))
        argv.extend(("--workdir", "/workspace", "--user", runtime_user, self.image))
        argv.extend(IDLE_COMMAND)
        self.creation_token = uuid.uuid4().hex
        argv[2:2] = ['--label', 'io.sang-coding-agent.creation=' + self.creation_token]
        self.container_id = self._checked(argv).strip()
        self.session_id = session_id
        self.mode = mode
        self.mount_source = workspace
        self.masks = tuple(normalized_masks)
        return self.container_id

    def start(self):
        if not self.container_id:
            raise DockerTransportError("container has not been created")
        self._checked(["docker", "start", self.container_id])

    def stop(self, grace_seconds=5):
        if self.container_id:
            self._checked(["docker", "stop", "--time", str(grace_seconds), self.container_id], timeout=grace_seconds + 10)

    def destroy(self):
        if self.container_id:
            self._checked(["docker", "rm", "--force", self.container_id])
            self.container_id = None

    def inspect(self) -> dict:
        if not self.container_id:
            raise DockerTransportError("container is unavailable")
        try:
            inspected = json.loads(self._checked(["docker", "inspect", self.container_id]))
        except json.JSONDecodeError as error:
            raise DockerTransportError("Docker inspect returned malformed JSON") from error
        if len(inspected) != 1 or inspected[0].get("Id") != self.container_id:
            raise DockerTransportError("Docker inspect container identity mismatch")
        item = inspected[0]
        config = item.get("Config", {})
        host = item.get("HostConfig", {})
        mounts = list(item.get("Mounts", []))
        mount = next((entry for entry in mounts if entry.get("Destination") == "/workspace"), {})
        return {
            "container_id": item["Id"],
            "running": bool(item.get("State", {}).get("Running", False)),
            "image_digest": config.get("Image", ""),
            "user": config.get("User", ""),
            "labels": dict(config.get("Labels") or {}),
            "mount_source": mount.get("Source"),
            "mount_rw": mount.get("RW"),
            "mounts": mounts,
            "network_mode": host.get("NetworkMode"),
            "read_only_rootfs": bool(host.get("ReadonlyRootfs", False)),
            "pids": host.get("PidsLimit"),
            "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"),
            "cpus": (host.get("NanoCpus") or 0) / 1_000_000_000,
            "cap_drop": list(host.get("CapDrop") or []),
            "security_opt": list(host.get("SecurityOpt") or []),
            'entrypoint': config.get('Entrypoint'),
            'command': config.get('Cmd'),
        }

    def verify_runtime(self, limits: ResourceLimits, workspace: Path) -> dict:
        evidence = self.inspect()
        expected = {
            "running": True,
            "image_digest": self.image,
            "user": "0:0" if self.mode == WorkspaceMode.LIVE else "65532:65532",
            "mount_source": str(self.daemon_source(Path(workspace))),
            "mount_rw": not self.action_profile,
            "network_mode": "none",
            "read_only_rootfs": True,
            "pids": limits.pids,
            "memory_bytes": limits.memory_bytes,
            "memory_swap_bytes": limits.memory_bytes,
            "cpus": limits.cpus,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise DockerPreflightError("sandbox runtime inspect contract mismatch")
        mount_evidence = {
            item.get("Destination"): item
            for item in evidence.get("mounts", [])
        }
        for entry, source in self.masks:
            item = mount_evidence.get(f"/workspace/{entry.path}", {})
            if item.get("Source") != str(self.daemon_source(source)) or item.get("RW") is not False:
                raise DockerPreflightError("sandbox runtime mask contract mismatch")
        if set(evidence.get("cap_drop", [])) != {"ALL"} or "no-new-privileges:true" not in evidence.get("security_opt", []):
            raise DockerPreflightError("sandbox runtime privilege contract mismatch")
        script = """import json
from pathlib import Path
status = {}
for line in Path('/proc/self/status').read_text().splitlines():
    if ':' in line:
        key, value = line.split(':', 1)
        status[key] = value.strip()
mounts = []
for line in Path('/proc/mounts').read_text().splitlines():
    fields = line.split()
    if len(fields) >= 4:
        mounts.append({'target': fields[1], 'options': fields[3].split(',')})
print(json.dumps({
    'tmpfs_bytes': __import__('os').statvfs('/tmp').f_blocks * __import__('os').statvfs('/tmp').f_frsize,
    'tmpfs_inodes': __import__('os').statvfs('/tmp').f_files,
    'nonewprivs': status.get('NoNewPrivs'),
    'capeff': status.get('CapEff'),
    'memory': Path('/sys/fs/cgroup/memory.max').read_text().strip(),
    'swap': Path('/sys/fs/cgroup/memory.swap.max').read_text().strip(),
    'pids': Path('/sys/fs/cgroup/pids.max').read_text().strip(),
    'cpu': Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
    'route_lines': len(Path('/proc/net/route').read_text().splitlines()),
    'mounts': mounts,
}))"""
        try:
            probe = json.loads(self._checked(["docker", "exec", self.container_id, "python", "-c", script]))
        except (DockerTransportError, json.JSONDecodeError) as error:
            raise DockerPreflightError("sandbox runtime probe failed") from error
        try:
            cpu_quota, cpu_period = probe.get("cpu", "0 0").split()
            mount_map = {item["target"]: set(item["options"]) for item in probe.get("mounts", [])}
            writable = {target for target, options in mount_map.items() if "rw" in options}
            allowed_writable = {
                "/workspace", "/tmp", "/dev", "/dev/pts", "/dev/shm", "/dev/mqueue",
                "/dev/null", "/dev/random", "/dev/full", "/dev/tty", "/dev/zero", "/dev/urandom",
                "/proc", "/proc/interrupts", "/proc/kcore", "/proc/keys", "/proc/timer_list",
            }
            valid = (
                probe.get("nonewprivs") == "1"
                and int(probe.get("capeff", "-1"), 16) == 0
                and probe.get("memory") == str(limits.memory_bytes)
                and probe.get("swap") in {"0", str(limits.memory_bytes)}
                and probe.get("pids") == str(limits.pids)
                and int(cpu_period) > 0
                and int(cpu_quota) / int(cpu_period) == limits.cpus
                and probe.get("route_lines") == 1
                and "ro" in mount_map.get("/", set())
                and {"rw", "nosuid", "nodev", "noexec"}.issubset(mount_map.get("/tmp", set()))
                and writable <= allowed_writable
            )
            if self.action_profile:
                valid = valid and 'ro' in mount_map.get('/workspace', set()) and probe.get('tmpfs_bytes') == limits.tmpfs_bytes and probe.get('tmpfs_inodes') == limits.tmpfs_inodes
        except (KeyError, TypeError, ValueError) as error:
            raise DockerPreflightError("sandbox runtime probe returned invalid evidence") from error
        if not valid:
            raise DockerPreflightError("sandbox runtime probe contract mismatch")
        return {"inspect": evidence, "probe": probe}

    def create_action(self, action_id, generation, limits):
        if not action_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in action_id):
            raise DockerPreflightError('invalid_action_id')
        self.action_profile = True
        return self.create(action_id, generation.digest, generation.directory, limits)

    def verify_action(self, limits, generation):
        if self.isolation_level != DockerIsolationLevel.ROOTLESS:
            raise DockerPreflightError('action_requires_rootless')
        evidence = self.verify_runtime(limits, generation.directory)
        inspection = evidence['inspect']
        if len(inspection['mounts']) != 1 or inspection['entrypoint'] != ['/usr/bin/env'] or inspection['command'] != IDLE_COMMAND:
            raise DockerPreflightError('action_profile_mismatch')
        self.verify_helpers()
        return evidence

    def verify_helpers(self):
        from sandbox.helpers import helper_path
        for name in ('sandbox_fs', 'sandbox_exec'):
            source = helper_path(name)
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            script = f"import hashlib; print(hashlib.sha256(open('/opt/coding-agent/bin/{name}.py','rb').read()).hexdigest())"
            actual = self._checked(['docker', 'exec', self.container_id, '/usr/local/bin/python', '-I', '-c', script]).strip()
            if actual != expected:
                raise DockerPreflightError('pinned_helper_mismatch')

    def revoke_action(self):
        if not self.container_id and not self.creation_token:
            return
        selection = ('label=io.sang-coding-agent.creation=' + self.creation_token
                     if self.creation_token else 'id=' + self.container_id)
        listing = ['docker', 'container', 'ls', '--all', '--quiet', '--no-trunc', '--filter', selection]
        # A create response can be lost after the daemon accepted the request.
        identities = [self.container_id] if self.container_id else self._checked(listing).split()
        for identity in identities:
            self._checked(['docker', 'rm', '--force', identity])
        remaining = self._checked(listing).strip()
        if remaining:
            raise DockerTransportError('action_quiescence_unknown')
        self.container_id = None
        self.creation_token = ''

    def exec_action(self, request, generation, limits):
        identity = ''
        try:
            identity = self.create_action(request.action_id, generation, limits)
            self.start()
            self.verify_action(limits, generation)
            data = self._helper('sandbox_exec', {'command': request.command, 'cwd': str(request.cwd),
                'timeout_seconds': min(request.timeout_seconds, limits.tool_timeout_seconds), 'output_bytes': limits.output_bytes}, min(request.timeout_seconds, limits.tool_timeout_seconds) + 2)
            if not isinstance(data, dict) or not isinstance(data.get('stdout'), str) or not isinstance(data.get('stderr'), str) or len((data['stdout'] + data['stderr']).encode()) > limits.output_bytes * 3:
                raise DockerTransportError('invalid_action_output')
            status = ExecutionStatus(data['status'])
            result = SandboxResult(status, data['stdout'], data['stderr'], data.get('exit_code'), ViolationStatus.UNKNOWN,
                request.action_id, identity, self.image, bool(data.get('truncated')), status.value, 'unknown', 'read_only', True)
        finally:
            self.revoke_action()
        return result

    def _stop_after_ambiguous_transport(self):
        try:
            self.stop()
            if self.inspect()["running"]:
                raise DockerTransportError("sandbox container is still running after stop")
        except DockerTransportError as error:
            raise DockerTransportError("sandbox outcome is ambiguous and stop could not be verified") from error

    def _helper(self, helper: str, payload: dict, timeout: float) -> dict:
        if not self.container_id:
            raise DockerTransportError("container is unavailable")
        try:
            output = self._checked(
                (["docker", "exec", "-i", '--user', '0:0', self.container_id, '/usr/local/bin/python', '-I', f"/opt/coding-agent/bin/{helper}.py"]
                 if helper == 'sandbox_fs' else ["docker", "exec", "-i", self.container_id, '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=/tmp', '/usr/local/bin/python', '-I', f"/opt/coding-agent/bin/{helper}.py"]),
                input=json.dumps(payload, separators=(",", ":")), timeout=timeout,
            )
            return json.loads(output)
        except (DockerTransportError, json.JSONDecodeError) as error:
            self._stop_after_ambiguous_transport()
            message = "sandbox helper returned malformed JSON" if isinstance(error, json.JSONDecodeError) else "sandbox execution outcome is ambiguous"
            raise DockerTransportError(message) from error

    def exec(self, request: ExecRequest) -> SandboxResult:
        data = self._helper("sandbox_exec", {
            "command": request.command, "cwd": str(request.cwd), "timeout_seconds": request.timeout_seconds,
        }, request.timeout_seconds + 2)
        status = ExecutionStatus(data["status"])
        if status == ExecutionStatus.TIMED_OUT:
            self._stop_after_ambiguous_transport()
        violation = ViolationStatus.DETECTED if status == ExecutionStatus.BLOCKED_BY_SANDBOX else ViolationStatus.UNKNOWN
        return SandboxResult(status, data.get("stdout", ""), data.get("stderr", ""), data.get("exit_code"), violation, self.session_id, self.container_id or "", self.image, bool(data.get("truncated")))

    def verify_file_adapter(self, limits, workspace, helper_digest):
        if self.isolation_level != DockerIsolationLevel.ROOTLESS:
            raise DockerPreflightError('safe file adapter requires rootless Docker')
        evidence = self.verify_runtime(limits, workspace)
        inspection = evidence['inspect']
        if len(inspection.get('mounts', ())) != 1 or inspection.get('entrypoint') != ['/usr/bin/env'] or inspection.get('command') != IDLE_COMMAND:
            raise DockerPreflightError('file_adapter_profile_mismatch')
        script = "import hashlib; print(hashlib.sha256(open('/opt/coding-agent/bin/sandbox_fs.py','rb').read()).hexdigest())"
        actual = self._checked(['docker', 'exec', self.container_id, '/usr/local/bin/python', '-I', '-c', script]).strip()
        if actual != helper_digest:
            raise DockerPreflightError('pinned file helper identity mismatch')
        self.verify_helpers()
        return {'file_adapter': True, 'helper_digest': actual, 'rootless': True, 'opaque_ro': True}

    def fs_call(self, payload: dict, *, grant=None) -> dict:
        if not isinstance(grant, dict):
            raise DockerPreflightError('controller file grant required')
        return self._helper("sandbox_fs", {'request': payload, 'grant': grant}, 65)
