"""Host control-plane change sealing and whole-set apply."""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from sandbox.models import ApplyResult, ChangeEntry, ChangeSet, SandboxPath
from sandbox.workspace import _HARD_PATTERNS, _matches

MAX_FILE_BYTES = 20 * 1024**2
MAX_TOTAL_BYTES = 100 * 1024**2
MAX_ENTRIES = 10_000
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255


class ChangeSetError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_baseline(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["path"]: item for item in data["included"] if item["kind"] in {"file", "symlink"}}


def _scan(root: Path, *, max_file_bytes=MAX_FILE_BYTES) -> dict[str, dict]:
    entries, seen = {}, set()
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
            raw = relative.encode("utf-8")
            if len(raw) > MAX_PATH_BYTES or any(len(part.encode("utf-8")) > MAX_COMPONENT_BYTES for part in PurePosixPath(relative).parts):
                raise ChangeSetError(f"path limit exceeded: {relative}")
            key = relative.casefold()
            if key in seen:
                raise ChangeSetError(f"case or Unicode collision: {relative}")
            seen.add(key)
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                entries[relative] = {"kind": "symlink", "link_target": os.readlink(path)}
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ChangeSetError(f"special file is forbidden: {relative}")
            if info.st_size > max_file_bytes:
                raise ChangeSetError(f"per-file change limit exceeded: {relative}")
            data = path.read_bytes()
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            entries[relative] = {"kind": "file", "sha256": _hash(data), "size": len(data), "mode": mode, "data": data}
    return entries


def _canonical(changes: tuple[ChangeEntry, ...], violations: tuple[str, ...], final_manifest_hash: str) -> bytes:
    payload = {
        "entries": [{**asdict(item), "path": str(item.path)} for item in changes],
        "violations": list(violations),
        "final_manifest_hash": final_manifest_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _preview(before: bytes, after: bytes, path: str) -> tuple[str, str | None]:
    binary = b"\x00" in after
    try:
        old_text, new_text = before.decode("utf-8"), after.decode("utf-8")
    except UnicodeDecodeError:
        binary = True
    if binary:
        return "binary", None
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    return "text", "\n".join(diff)


def build_changeset(paths, *, max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES, max_entries=MAX_ENTRIES) -> ChangeSet:
    baseline = _load_baseline(paths.baseline_manifest)
    final = _scan(paths.workspace, max_file_bytes=max_file_bytes)
    changes, violations, changed_bytes = [], [], 0
    for path in sorted(set(baseline) | set(final)):
        before, after = baseline.get(path), final.get(path)
        if before == after or (before and after and all(before.get(k) == after.get(k) for k in ("kind", "sha256", "mode", "link_target"))):
            continue
        operation = "create" if before is None else "delete" if after is None else "modify"
        if after and after["kind"] == "file":
            changed_bytes += after["size"]
            if changed_bytes > max_total_bytes:
                raise ChangeSetError("total changed content limit exceeded")
        if _matches(path, _HARD_PATTERNS):
            violations.append(f"denylisted path created or modified: {path}")
        if (before and before["kind"] == "symlink") or (after and after["kind"] == "symlink"):
            violations.append(f"new, deleted, or changed symlink is forbidden: {path}")
            kind, preview = "symlink", None
        else:
            before_data = b"" if before is None else (paths.workspace / path).read_bytes() if False else _baseline_bytes(paths, path)
            after_data = b"" if after is None else after["data"]
            kind, preview = _preview(before_data, after_data, path)
        changes.append(ChangeEntry(
            SandboxPath(path), operation, kind,
            before_sha256=before.get("sha256") if before else None,
            after_sha256=after.get("sha256") if after else None,
            before_mode=before.get("mode") if before else None,
            after_mode=after.get("mode") if after else None,
            size=after.get("size", 0) if after else 0,
            preview=preview,
        ))
    if len(changes) > max_entries:
        raise ChangeSetError("changed entry limit exceeded")
    manifest_payload = [{k: v for k, v in item.items() if k != "data"} | {"path": path} for path, item in sorted(final.items())]
    final_hash = _hash(json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode())
    changes_tuple, violations_tuple = tuple(changes), tuple(violations)
    digest = None if violations else _hash(_canonical(changes_tuple, violations_tuple, final_hash))
    result = ChangeSet(changes_tuple, violations_tuple, digest, final_hash, not violations)
    _persist_changeset(paths, result, manifest_payload)
    return result


def _baseline_bytes(paths, relative: str) -> bytes:
    command = ["git", f"--git-dir={paths.shadow_git}", "show", f"HEAD:{relative}"]
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode:
        raise ChangeSetError(f"cannot read baseline content: {relative}")
    return result.stdout


def _persist_changeset(paths, changes: ChangeSet, manifest: list[dict]) -> None:
    payload = {**asdict(changes), "entries": [{**asdict(e), "path": str(e.path)} for e in changes.entries]}
    for path, value in ((paths.final_manifest, manifest), (paths.changeset, payload)):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)


def load_changeset(paths) -> ChangeSet:
    try:
        payload = json.loads(paths.changeset.read_text(encoding="utf-8"))
        entries = tuple(ChangeEntry(
            path=SandboxPath(item["path"]),
            operation=item["operation"],
            kind=item["kind"],
            before_sha256=item.get("before_sha256"),
            after_sha256=item.get("after_sha256"),
            before_mode=item.get("before_mode"),
            after_mode=item.get("after_mode"),
            size=item.get("size", 0),
            preview=item.get("preview"),
        ) for item in payload["entries"])
        return ChangeSet(
            entries=entries,
            violations=tuple(payload["violations"]),
            change_set_hash=payload.get("change_set_hash"),
            final_manifest_hash=payload["final_manifest_hash"],
            approvable=bool(payload["approvable"]),
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        raise ChangeSetError("persisted sealed change set is invalid") from error


def verify_sealed_changeset(paths, changes: ChangeSet) -> bool:
    try:
        current = build_changeset(paths)
    except ChangeSetError:
        return False
    return current.approvable and current.change_set_hash == changes.change_set_hash


def _current_entry(root: Path, relative: str) -> dict | None:
    parts = PurePosixPath(relative).parts
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ChangeSetError(f"parent component is unsafe: {relative}")
    target = root / relative
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ChangeSetError(f"target type conflict: {relative}")
    data = target.read_bytes()
    return {"sha256": _hash(data), "mode": 0o755 if info.st_mode & 0o111 else 0o644}


def _check_conflicts(source: Path, changes: ChangeSet) -> None:
    for entry in changes.entries:
        current = _current_entry(source, str(entry.path))
        if entry.before_sha256 is None:
            if current is not None:
                raise ChangeSetError(f"workspace conflict: {entry.path} was created on host")
        elif current is None or current["sha256"] != entry.before_sha256 or current["mode"] != entry.before_mode:
            raise ChangeSetError(f"workspace conflict: {entry.path} changed on host")


@contextmanager
def _workspace_lock(paths):
    lock_path = paths.root / f"workspace-{paths.workspace_identity}.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _journal_write(path: Path, payload: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.coding-agent-",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            os.fchmod(output_stream.fileno(), mode)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def apply_changeset(paths, source_workspace, changes: ChangeSet, approved_hash: str, note: str, *, fail_after: int | None = None) -> ApplyResult:
    if not note.strip():
        raise ChangeSetError("approval note is required")
    if not changes.approvable or not approved_hash or approved_hash != changes.change_set_hash:
        raise ChangeSetError("approval hash does not match the whole change set")
    if not verify_sealed_changeset(paths, changes):
        raise ChangeSetError("sealed change set hash is no longer current")
    source = Path(source_workspace).resolve(strict=True)
    journal_path = paths.rollback / "apply-journal.json"
    backup_root = paths.rollback / "files"
    staging = paths.rollback / "staging"
    shutil.rmtree(backup_root, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    backup_root.mkdir(mode=0o700, parents=True)
    staging.mkdir(mode=0o700, parents=True)
    for entry in changes.entries:
        if entry.operation != "delete":
            target = staging / str(entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = paths.workspace / str(entry.path)
            if source_file.is_symlink() or not source_file.is_file():
                raise ChangeSetError(f"staging source is unsafe: {entry.path}")
            shutil.copyfile(source_file, target)
            target.chmod(entry.after_mode)
            if _hash(target.read_bytes()) != entry.after_sha256:
                raise ChangeSetError(f"staging hash mismatch: {entry.path}")
    applied = []
    journal = {"state": "started", "hash": approved_hash, "note": note, "operations": []}
    with _workspace_lock(paths):
        _check_conflicts(source, changes)
        _journal_write(journal_path, journal)
        try:
            for index, entry in enumerate(changes.entries, 1):
                relative = str(entry.path)
                destination = source / relative
                _current_entry(source, relative)
                backup = backup_root / relative
                existed = destination.exists()
                if existed:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(destination, backup)
                    backup.chmod(entry.before_mode)
                journal["operations"].append({
                    "path": relative,
                    "existed": existed,
                    "mode": entry.before_mode,
                })
                _journal_write(journal_path, journal)
                if entry.operation == "delete":
                    destination.unlink()
                else:
                    _atomic_copy(staging / relative, destination, entry.after_mode)
                applied.append(entry.path)
                if fail_after == index:
                    raise OSError("injected apply failure")
            for entry in changes.entries:
                current = _current_entry(source, str(entry.path))
                if entry.operation == "delete":
                    if current is not None:
                        raise OSError(f"delete verification failed: {entry.path}")
                elif current is None or current["sha256"] != entry.after_sha256 or current["mode"] != entry.after_mode:
                    raise OSError(f"post-apply verification failed: {entry.path}")
            journal["state"] = "completed"
            _journal_write(journal_path, journal)
            shutil.rmtree(backup_root)
            shutil.rmtree(staging)
            journal_path.unlink()
            return ApplyResult(True, approved_hash, tuple(applied), message="whole change set applied")
        except BaseException:
            rollback_ok = _rollback(source, backup_root, journal["operations"])
            if rollback_ok:
                journal_path.unlink(missing_ok=True)
                shutil.rmtree(backup_root, ignore_errors=True)
                shutil.rmtree(staging, ignore_errors=True)
            else:
                journal["state"] = "recovery_required"
                _journal_write(journal_path, journal)
            raise


def _rollback(source: Path, backup_root: Path, operations: list[dict]) -> bool:
    try:
        for operation in reversed(operations):
            destination = source / operation["path"]
            if destination.exists() and not destination.is_symlink():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if operation["existed"]:
                backup = backup_root / operation["path"]
                _atomic_copy(backup, destination, operation["mode"])
        return True
    except OSError:
        return False


def recover_incomplete_apply(paths, source_workspace) -> bool:
    journal_path = paths.rollback / "apply-journal.json"
    if not journal_path.exists():
        return False
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("state") == "completed":
        journal_path.unlink()
        return False
    if not _rollback(Path(source_workspace).resolve(strict=True), paths.rollback / "files", journal.get("operations", [])):
        raise ChangeSetError("unfinished apply requires manual recovery")
    journal_path.unlink()
    shutil.rmtree(paths.rollback / "files", ignore_errors=True)
    shutil.rmtree(paths.rollback / "staging", ignore_errors=True)
    return True
