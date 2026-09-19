from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import build_changeset
from sandbox.models import ManifestEntry, ResourceLimits, SandboxPath, SandboxStatus, WorkspaceMode
from sandbox.workspace import prepare_live_workspace, prepare_workspace


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
        mode: WorkspaceMode = WorkspaceMode.LIVE,
        free_space_floor_bytes=5 * 1024**3,
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
        if paths.metadata.exists():
            data = json.loads(paths.metadata.read_text())
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
        }
        temporary = self.paths.metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.paths.metadata)
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
        self._persist()

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
        if self.mode == WorkspaceMode.LIVE:
            raise SessionError("live workspace changes are already applied")
        if self.status == SandboxStatus.RUNNING:
            self.stop("prepare_changes")
        if self.status != SandboxStatus.STOPPED:
            raise SessionError("sandbox must be stopped before sealing changes")
        changes = build_changeset(self.paths)
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
        if self.status != SandboxStatus.RUNNING:
            raise SessionError("sandbox is not running")
        return self.backend.exec(request)

    def fs_call(self, request):
        if self.status != SandboxStatus.RUNNING:
            raise SessionError("sandbox is not running")
        return self.backend.fs_call(request)

    def fingerprint(self, relative_path):
        return self.fs_call({"operation": "fingerprint", "path": str(relative_path)}).get("sha256")


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
