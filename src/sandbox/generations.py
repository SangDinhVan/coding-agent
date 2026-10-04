"""Bounded no-follow copies and controller-private immutable input generations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import time
from dataclasses import dataclass
from pathlib import Path

from runtime.safety import digest_json
from sandbox.models import ManifestEntry, SandboxPath
from core.paths import open_directory


class GenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Generation:
    digest: str
    directory: Path
    manifest: tuple[ManifestEntry, ...]


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def manifest_data(entries):
    return [{'path': str(item.path), 'kind': item.kind, 'mode': item.mode,
             'size': item.size, 'sha256': item.sha256} for item in entries]


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_tree(source: Path, destination: Path, *, max_bytes, max_entries, exclude=None, max_file_bytes=20*1024**2, deadline_seconds=30):
    """Keep all source traversal relative to open directories; never dereference links."""
    included, excluded, seen = [], [], set()
    total, count = 0, 0
    deadline = time.monotonic() + deadline_seconds
    root_fd = open_directory(source)

    def walk(source_fd, target, relative=''):
        nonlocal total, count
        before_dir = os.fstat(source_fd)
        names = []
        with os.scandir(source_fd) as iterator:
            for entry in iterator:
                if time.monotonic() > deadline:
                    raise GenerationError('harvest_deadline')
                count += 1
                if count > max_entries:
                    raise GenerationError('entries limit exceeded')
                names.append(entry.name)
        for name in sorted(names):
            raw = f'{relative}/{name}' if relative else name
            normalized = unicodedata.normalize('NFC', raw)
            path = SandboxPath(normalized)
            key = normalized.casefold()
            if key in seen:
                raise GenerationError('case or Unicode path collision')
            seen.add(key)
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            reason = exclude(normalized) if exclude else None
            if reason:
                excluded.append(ManifestEntry(path, 'excluded', reason=reason))
                continue
            child_target = target / unicodedata.normalize('NFC', name)
            mode = stat.S_IMODE(info.st_mode) & 0o777
            if stat.S_ISLNK(info.st_mode):
                raise GenerationError('symlink admission forbidden')
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) or mode & 0o002:
                raise GenerationError('unsafe_mode')
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd)
                try:
                    if identity(info) != identity(os.fstat(fd)):
                        raise GenerationError('source directory changed')
                    child_target.mkdir(mode=0o700)
                    included.append(ManifestEntry(path, 'directory', mode=mode))
                    walk(fd, child_target, raw)
                    child_target.chmod(mode)
                    fsync_directory(child_target)
                    if identity(info) != identity(os.stat(name, dir_fd=source_fd, follow_symlinks=False)):
                        raise GenerationError('source directory changed')
                finally:
                    os.close(fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > max_file_bytes:
                    raise GenerationError('per_file_limit')
                if info.st_nlink != 1:
                    raise GenerationError('hardlink admission forbidden')
                total += info.st_size
                if total > max_bytes:
                    raise GenerationError('baseline bytes limit exceeded')
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
                try:
                    if identity(info) != identity(os.fstat(fd)):
                        raise GenerationError('source file changed')
                    digest, copied = hashlib.sha256(), 0
                    with child_target.open('xb') as output:
                        while chunk := os.read(fd, min(1024**2, max_bytes - (total - info.st_size) - copied + 1)):
                            if time.monotonic() > deadline:
                                raise GenerationError('harvest_deadline')
                            copied += len(chunk)
                            if copied > info.st_size or total - info.st_size + copied > max_bytes:
                                raise GenerationError('source file changed or bytes limit exceeded')
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fchmod(output.fileno(), mode)
                        os.fsync(output.fileno())
                    if copied != info.st_size or identity(info) != identity(os.fstat(fd)) or identity(info) != identity(os.stat(name, dir_fd=source_fd, follow_symlinks=False)):
                        raise GenerationError('source file changed')
                    included.append(ManifestEntry(path, 'file', mode=mode, size=copied, sha256=digest.hexdigest()))
                finally:
                    os.close(fd)
            else:
                raise GenerationError('unsupported inode type')
        if identity(before_dir) != identity(os.fstat(source_fd)):
            raise GenerationError('source directory changed')
        fsync_directory(target)

    try:
        walk(root_fd, destination)
        if identity(os.fstat(root_fd)) != identity(os.stat(source, follow_symlinks=False)):
            raise GenerationError('source root changed')
    except (OSError, ValueError) as error:
        raise GenerationError('unsafe or changed source') from error
    finally:
        os.close(root_fd)
    return tuple(included), tuple(excluded), total


def seal_generation(workspace: Path, destination_root: Path, *, max_bytes: int, max_entries: int, **budgets) -> Generation:
    workspace, destination_root = Path(workspace), Path(destination_root)
    if destination_root.resolve().is_relative_to(workspace.resolve()) or workspace.resolve().is_relative_to(destination_root.resolve()):
        raise GenerationError('generation/source overlap')
    destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.seal-', dir=destination_root))
    try:
        entries, _, _ = copy_tree(workspace, stage, max_bytes=max_bytes, max_entries=max_entries, **budgets)
        digest = digest_json(manifest_data(entries))
        destination = destination_root / digest
        if destination.exists():
            candidate = Generation(digest, destination, entries)
            if not verify_generation(candidate):
                raise GenerationError('existing generation changed')
            shutil.rmtree(stage)
        else:
            os.rename(stage, destination)
            fsync_directory(destination_root)
        return Generation(digest, destination, entries)
    except (OSError, ValueError) as error:
        raise GenerationError('generation could not be sealed') from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_generation(generation: Generation) -> bool:
    try:
        # Reuse the same traversal and identity checks; copies stay private and bounded.
        with tempfile.TemporaryDirectory(dir=generation.directory.parent, prefix='.verify-') as folder:
            entries, _, _ = copy_tree(generation.directory, Path(folder),
                                     max_bytes=sum(item.size or 0 for item in generation.manifest),
                                     max_entries=len(generation.manifest))
        return entries == generation.manifest and digest_json(manifest_data(entries)) == generation.digest
    except (OSError, ValueError, GenerationError):
        return False
