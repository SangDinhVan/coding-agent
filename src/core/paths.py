from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def open_directory(path, *, create=False):
    """Open an absolute directory without following links in any component."""
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in {'.', '..'}:
                raise ValueError('unsafe_directory_path')
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def workspace_identity(source_workspace: str | Path) -> str:
    source = Path(source_workspace).resolve(strict=True)
    if not source.is_dir():
        raise ValueError("source workspace must be a directory")
    identity_material = f"{source}:{source.stat().st_dev}:{source.stat().st_ino}".encode()
    return hashlib.sha256(identity_material).hexdigest()


def project_memory_path(state_root: str | Path, identity: str) -> Path:
    return Path(state_root).expanduser().resolve() / "projects" / identity / "PROJECT.md"


def checkpoint_path(state_root: str | Path, session_id: str) -> Path:
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session ID")
    return Path(state_root).expanduser().resolve() / "checkpoints" / f"{session_id}.json"


@dataclass(frozen=True)
class ControlPaths:
    root: Path
    session_id: str
    session_dir: Path
    workspace: Path
    shadow_git: Path
    rollback: Path
    metadata: Path
    heartbeat: Path
    baseline_manifest: Path
    final_manifest: Path
    changeset: Path
    mask_file: Path
    mask_directory: Path
    workspace_identity: str
    generations: Path
    checkpoints: Path

    @staticmethod
    def default_root() -> Path:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "sang-coding-agent"

    @classmethod
    def create(cls, root: str | Path, session_id: str, source_workspace: str | Path) -> "ControlPaths":
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session ID")
        source = Path(source_workspace).resolve(strict=True)
        identity = workspace_identity(source)
        root_path = Path(root).expanduser().resolve()
        if root_path.is_relative_to(source) or source.is_relative_to(root_path):
            raise ValueError('source/state root overlap')
        session_dir = root_path / "sandboxes" / session_id
        for directory in (root_path, root_path / "sandboxes", session_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        workspace = session_dir / "workspace"
        rollback = session_dir / "rollback"
        for directory in (workspace, rollback):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        mask_directory = session_dir / "mask-directory"
        mask_file = session_dir / "mask-file"
        mask_directory.mkdir(mode=0o700, exist_ok=True)
        mask_directory.chmod(0o700)
        mask_file.touch(mode=0o600, exist_ok=True)
        mask_file.chmod(0o600)
        return cls(
            root=root_path,
            session_id=session_id,
            session_dir=session_dir,
            workspace=workspace,
            shadow_git=session_dir / "shadow.git",
            rollback=rollback,
            metadata=session_dir / "metadata.json",
            heartbeat=session_dir / "heartbeat",
            baseline_manifest=session_dir / "baseline-manifest.json",
            final_manifest=session_dir / "final-manifest.json",
            changeset=session_dir / "changeset.json",
            mask_file=mask_file,
            mask_directory=mask_directory,
            workspace_identity=identity,
            generations=session_dir / 'generations',
            checkpoints=session_dir / 'checkpoints',
        )
