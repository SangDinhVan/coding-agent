from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import unicodedata
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from core.paths import ControlPaths
from sandbox.models import ManifestEntry, SandboxPath, SnapshotManifest


class SnapshotError(RuntimeError):
    pass


_HARD_PATTERNS = (
    ".git", ".git/*", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".ssh", ".ssh/*", ".aws/credentials",
    ".aws/config", ".npmrc", ".netrc", ".pypirc", ".docker/config.json",
    "*credentials*.json", "*secret*.json", "*token*.json", ".terraform", ".terraform/*",
    "kubeconfig", ".kube", ".kube/*",
)
_PERFORMANCE_PATTERNS = (
    ".venv", ".venv/*", "node_modules", "node_modules/*", "__pycache__", "*/__pycache__",
    "*/__pycache__/*", "*.pyc", "*.pyo", ".pytest_cache", ".pytest_cache/*", ".mypy_cache",
    ".mypy_cache/*", ".ruff_cache", ".ruff_cache/*", "build", "build/*", "dist", "dist/*",
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    lowered = path.casefold()
    base = PurePosixPath(lowered).name
    return any(fnmatch.fnmatchcase(lowered, p.casefold()) or fnmatch.fnmatchcase(base, p.casefold()) for p in patterns)


def _read_patterns(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    result = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            raise SnapshotError(f".agentignore negation is forbidden at line {line_number}")
        if line.startswith("/") or "\x00" in line or ".." in PurePosixPath(line).parts:
            raise SnapshotError(f"invalid .agentignore pattern at line {line_number}")
        result.append(line.rstrip("/") + ("/*" if line.endswith("/") else ""))
        if line.endswith("/"):
            result.append(line.rstrip("/"))
    return tuple(result)


def _git_ignored(source: Path) -> set[str]:
    if not (source / ".gitignore").is_file():
        return set()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=", "-c", "core.hooksPath=/dev/null", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=source, env=env, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return {line.rstrip("/") for line in result.stdout.splitlines() if line}
    patterns = []
    for raw in (source / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "!")):
            patterns.extend((line.rstrip("/"), line.rstrip("/") + "/*"))
    ignored = set()
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            rel = (Path(root) / name).relative_to(source).as_posix()
            if _matches(rel, tuple(patterns)):
                ignored.add(rel)
    return ignored


def _entry(path: str, kind: str, **kwargs) -> ManifestEntry:
    return ManifestEntry(path=SandboxPath(path), kind=kind, **kwargs)


def _inside_relative_symlink(relative: str, target: str) -> bool:
    if not target or os.path.isabs(target):
        return False
    stack = list(PurePosixPath(relative).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return False
            stack.pop()
        else:
            stack.append(part)
    return True


def _write_manifest(path: Path, manifest: SnapshotManifest) -> None:
    def encode(value):
        if isinstance(value, SandboxPath):
            return str(value)
        raise TypeError(type(value).__name__)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(manifest), default=encode, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def prepare_workspace(
    source: str | Path,
    paths: ControlPaths,
    *,
    baseline_limit_bytes: int = 2 * 1024**3,
    free_space_floor_bytes: int = 5 * 1024**3,
) -> SnapshotManifest:
    source = Path(source).resolve(strict=True)
    if paths.workspace.exists():
        shutil.rmtree(paths.workspace)
    paths.workspace.mkdir(mode=0o777, parents=True)
    paths.workspace.chmod(0o777)
    if shutil.disk_usage(paths.session_dir).free < free_space_floor_bytes:
        raise SnapshotError("host free space is below the safety floor")
    agent_patterns = _read_patterns(source / ".agentignore")
    ignored = _git_ignored(source)
    included, excluded, rejected = [], [], []
    total = 0
    seen = set()

    for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        names = sorted(dirs + files)
        dirs[:] = []
        for name in names:
            current = root_path / name
            raw_relative = current.relative_to(source).as_posix()
            relative = unicodedata.normalize("NFC", raw_relative)
            try:
                normalized = SandboxPath(relative)
            except ValueError as error:
                raise SnapshotError(f"invalid path {raw_relative!r}: {error}") from error
            collision = str(normalized).casefold()
            if collision in seen:
                raise SnapshotError(f"case or Unicode path collision: {relative}")
            seen.add(collision)
            mode = os.lstat(current).st_mode
            if _matches(relative, _HARD_PATTERNS):
                excluded.append(_entry(relative, "excluded", reason="hard_security_denylist"))
                continue
            if _matches(relative, agent_patterns):
                excluded.append(_entry(relative, "excluded", reason="project_agentignore"))
                continue
            if relative in ignored or _matches(relative, _PERFORMANCE_PATTERNS):
                excluded.append(_entry(relative, "excluded", reason="performance_exclude"))
                continue
            destination = paths.workspace / relative
            if stat.S_ISLNK(mode):
                target = os.readlink(current)
                if not _inside_relative_symlink(relative, target):
                    rejected.append(_entry(relative, "symlink", link_target=target, reason="symlink_escapes_workspace"))
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(target)
                included.append(_entry(relative, "symlink", link_target=target))
            elif stat.S_ISDIR(mode):
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(0o777)
                included.append(_entry(relative, "directory", mode=0o755))
                dirs.append(name)
            elif stat.S_ISREG(mode):
                size = os.lstat(current).st_size
                total += size
                if total > baseline_limit_bytes:
                    raise SnapshotError("baseline snapshot exceeds configured limit")
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with current.open("rb") as src, destination.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        digest.update(chunk)
                        dst.write(chunk)
                safe_mode = 0o755 if mode & 0o111 else 0o644
                destination.chmod(0o777 if safe_mode == 0o755 else 0o666)
                if os.lstat(current).st_size != size or hashlib.sha256(destination.read_bytes()).hexdigest() != digest.hexdigest():
                    raise SnapshotError(f"source changed while copying: {relative}")
                included.append(_entry(relative, "file", mode=safe_mode, size=size, sha256=digest.hexdigest()))
            else:
                rejected.append(_entry(relative, "special", reason="unsupported_inode_type"))

    if rejected:
        reasons = ", ".join(f"{item.path}: {item.reason}" for item in rejected)
        raise SnapshotError(f"snapshot rejected unsupported or unsafe entries: {reasons}")
    manifest = SnapshotManifest(paths.workspace_identity, tuple(included), tuple(excluded), tuple(rejected), total)
    if paths.shadow_git.exists():
        shutil.rmtree(paths.shadow_git)
    git_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Sandbox Baseline",
        "GIT_AUTHOR_EMAIL": "sandbox@invalid",
        "GIT_COMMITTER_NAME": "Sandbox Baseline",
        "GIT_COMMITTER_EMAIL": "sandbox@invalid",
    }
    prefix = ["git", f"--git-dir={paths.shadow_git}", f"--work-tree={paths.workspace}", "-c", "core.hooksPath=/dev/null"]
    commands = (
        ["git", "init", "--bare", str(paths.shadow_git)],
        prefix + ["add", "--all"],
        prefix + ["commit", "--allow-empty", "--no-gpg-sign", "-m", "sandbox baseline"],
    )
    for command in commands:
        result = subprocess.run(command, env=git_env, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise SnapshotError(f"cannot create shadow Git baseline: {result.stderr.strip()}")
    _write_manifest(paths.baseline_manifest, manifest)
    return manifest


def prepare_live_workspace(
    source: str | Path,
    paths: ControlPaths,
    *,
    free_space_floor_bytes: int = 5 * 1024**3,
) -> SnapshotManifest:
    """Inspect a live workspace and persist the paths Docker must mask read-only."""
    source = Path(source).resolve(strict=True)
    if shutil.disk_usage(paths.session_dir).free < free_space_floor_bytes:
        raise SnapshotError("host free space is below the safety floor")
    agent_patterns = _read_patterns(source / ".agentignore")
    included, excluded, rejected = [], [], []
    total = 0
    seen = set()

    for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        kept_dirs = []
        for name in sorted(dirs + files):
            current = root_path / name
            relative = unicodedata.normalize("NFC", current.relative_to(source).as_posix())
            try:
                normalized = SandboxPath(relative)
            except ValueError as error:
                raise SnapshotError(f"invalid path {relative!r}: {error}") from error
            collision = str(normalized).casefold()
            if collision in seen:
                raise SnapshotError(f"case or Unicode path collision: {relative}")
            seen.add(collision)
            mode = os.lstat(current).st_mode
            is_directory = stat.S_ISDIR(mode)
            reason = None
            if _matches(relative, _HARD_PATTERNS):
                reason = "hard_security_denylist"
            elif _matches(relative, agent_patterns):
                reason = "project_agentignore"
            elif _matches(relative, _PERFORMANCE_PATTERNS):
                reason = "performance_exclude"
            if reason:
                excluded.append(_entry(relative, "directory" if is_directory else "file", reason=reason))
                continue
            if stat.S_ISLNK(mode):
                target = os.readlink(current)
                if not _inside_relative_symlink(relative, target):
                    rejected.append(_entry(relative, "symlink", link_target=target, reason="symlink_escapes_workspace"))
                else:
                    included.append(_entry(relative, "symlink", link_target=target))
            elif is_directory:
                included.append(_entry(relative, "directory", mode=0o755))
                kept_dirs.append(name)
            elif stat.S_ISREG(mode):
                size = os.lstat(current).st_size
                total += size
                included.append(_entry(relative, "file", mode=0o755 if mode & 0o111 else 0o644, size=size))
            else:
                rejected.append(_entry(relative, "special", reason="unsupported_inode_type"))
        dirs[:] = kept_dirs

    if rejected:
        reasons = ", ".join(f"{item.path}: {item.reason}" for item in rejected)
        raise SnapshotError(f"live workspace rejected unsupported or unsafe entries: {reasons}")
    manifest = SnapshotManifest(paths.workspace_identity, tuple(included), tuple(excluded), tuple(rejected), total)
    _write_manifest(paths.baseline_manifest, manifest)
    return manifest
