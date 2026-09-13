from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath


class SandboxStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    SEALED = "sealed"
    DESTROYED = "destroyed"
    ERROR = "error"


class WorkspaceMode(str, Enum):
    LIVE = "live"
    SHADOW = "shadow"


class SandboxPath(str):
    """A normalized POSIX path relative to the sandbox workspace."""

    def __new__(cls, value: str):
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
            raise ValueError("sandbox path must be a non-empty POSIX path")
        normalized = unicodedata.normalize("NFC", value)
        path = PurePosixPath(normalized)
        if path.is_absolute() or normalized != path.as_posix() or ".." in path.parts:
            raise ValueError("sandbox path must be normalized and workspace-relative")
        return str.__new__(cls, normalized)


@dataclass(frozen=True)
class ResourceLimits:
    cpus: float = 2.0
    memory_bytes: int = 2 * 1024**3
    pids: int = 256
    tmpfs_bytes: int = 512 * 1024**2
    tool_timeout_seconds: int = 60
    output_bytes: int = 10 * 1024**2
    workspace_growth_bytes: int = 4 * 1024**3

    def __post_init__(self) -> None:
        checks = (
            (0 < self.cpus <= 4, "CPU limit must be in (0, 4]"),
            (0 < self.memory_bytes <= 4 * 1024**3, "memory limit must be in (0, 4 GiB]"),
            (0 < self.pids <= 512, "PIDs limit must be in (0, 512]"),
            (0 < self.tmpfs_bytes <= 1024**3, "tmpfs limit must be in (0, 1 GiB]"),
            (0 < self.tool_timeout_seconds <= 600, "timeout must be in (0, 600 seconds]"),
            (0 < self.output_bytes <= 10 * 1024**2, "output limit must be in (0, 10 MiB]"),
            (0 < self.workspace_growth_bytes <= 8 * 1024**3, "workspace growth limit must be in (0, 8 GiB]"),
        )
        for valid, message in checks:
            if not valid:
                raise ValueError(message)


@dataclass(frozen=True)
class ManifestEntry:
    path: SandboxPath
    kind: str
    mode: int | None = None
    size: int | None = None
    sha256: str | None = None
    link_target: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SnapshotManifest:
    source_identity: str
    included: tuple[ManifestEntry, ...]
    excluded: tuple[ManifestEntry, ...]
    rejected: tuple[ManifestEntry, ...]
    total_bytes: int

class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED_BY_SANDBOX = "blocked_by_sandbox"
    RUNTIME_ERROR = "runtime_error"
    TIMED_OUT = "timed_out"
    TRANSPORT_ERROR = "transport_error"


class ViolationStatus(str, Enum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecRequest:
    action_id: str
    command: str
    cwd: SandboxPath = SandboxPath(".")
    timeout_seconds: float = 60

    def __post_init__(self):
        if not self.action_id or not isinstance(self.command, str):
            raise ValueError("execution requires action ID and command")
        if not 0 < self.timeout_seconds <= 600:
            raise ValueError("execution timeout exceeds hard ceiling")


@dataclass(frozen=True)
class SandboxResult:
    status: ExecutionStatus
    stdout: str
    stderr: str
    exit_code: int | None
    violation_status: ViolationStatus
    sandbox_session_id: str
    container_id: str
    image_digest: str
    truncated: bool = False

    @property
    def success(self):
        return self.status == ExecutionStatus.SUCCESS


@dataclass(frozen=True)
class ChangeEntry:
    path: SandboxPath
    operation: str
    kind: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    before_mode: int | None = None
    after_mode: int | None = None
    size: int = 0
    preview: str | None = None


@dataclass(frozen=True)
class ChangeSet:
    entries: tuple[ChangeEntry, ...]
    violations: tuple[str, ...]
    change_set_hash: str | None
    final_manifest_hash: str
    approvable: bool


@dataclass(frozen=True)
class ApplyResult:
    success: bool
    change_set_hash: str
    applied_paths: tuple[SandboxPath, ...] = ()
    rolled_back: bool = False
    recovery_required: bool = False
    message: str = ""
