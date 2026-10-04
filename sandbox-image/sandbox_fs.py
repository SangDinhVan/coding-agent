#!/usr/bin/env python3
"""Descriptor-relative filesystem helper executed inside the sandbox namespace."""
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import PurePosixPath

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ENTRIES = 10000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _validate_request(request):
    if not isinstance(request, dict):
        raise PermissionError('invalid request')
    fields = {
        'read': {'operation', 'path'}, 'fingerprint': {'operation', 'path'},
        'write': {'operation', 'path', 'content'},
        'edit': {'operation', 'path', 'old_string', 'new_string'},
    }.get(request.get('operation'))
    if fields is None or request.keys() != fields or not all(isinstance(value, str) for value in request.values()):
        raise PermissionError('invalid request schema')
    _parts(request['path'])


def _parents(workspace, path):
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    result = []
    try:
        info = os.fstat(fd)
        result.append([info.st_dev, info.st_ino])
        missing = False
        for part in _parts(path)[:-1]:
            if missing:
                result.append(None)
                continue
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                result.append(None)
                missing = True
                continue
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            result.append([info.st_dev, info.st_ino])
        return result
    finally:
        os.close(fd)


def target_state(workspace, path, max_bytes=MAX_FILE_BYTES):
    try:
        parent, name = _parent_fd(workspace, path)
    except FileNotFoundError:
        return {'kind': 'absent'}
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return {'kind': 'absent'}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o7000:
                raise PermissionError('unsafe target type, links or mode')
            digest, size = hashlib.sha256(), 0
            while chunk := os.read(fd, min(1024**2, max_bytes - size + 1)):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('file limit exceeded')
                digest.update(chunk)
            after = os.fstat(fd)
            if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeError('target changed concurrently')
            return {'kind': 'file', 'mode': stat.S_IMODE(info.st_mode), 'size': size, 'sha256': digest.hexdigest()}
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _budget(workspace, max_total_bytes, max_entries):
    count, total = 0, 0
    for root, dirs, files in os.walk(workspace, followlinks=False):
        for name in dirs + files:
            info = os.stat(os.path.join(root, name), follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise PermissionError('unsafe workspace entry')
            count += 1
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or info.st_mode & 0o7000:
                    raise PermissionError('unsafe workspace file')
                total += info.st_size
            if count > max_entries or total > max_total_bytes:
                raise ValueError('workspace budget exceeded')
    return count, total


def make_grant(request, workspace, *, max_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES, max_entries=MAX_ENTRIES, protected=False):
    _validate_request(request)
    for value, ceiling in ((max_bytes, MAX_FILE_BYTES), (max_total_bytes, MAX_TOTAL_BYTES), (max_entries, MAX_ENTRIES)):
        if type(value) is not int or not 0 < value <= ceiling:
            raise ValueError('invalid grant budget')
    before = target_state(workspace, request['path'], max_bytes)
    count, total = _budget(workspace, max_total_bytes, max_entries)
    if request['operation'] in {'write', 'edit'}:
        if request['operation'] == 'write':
            data = request['content'].encode()
        else:
            content = _read(workspace, request['path'], max_bytes).decode('utf-8')
            old = request['old_string']
            if not old or content.count(old) != 1:
                raise ValueError('old_string must match uniquely')
            data = content.replace(old, request['new_string'], 1).encode()
        missing_parents = sum(item is None for item in _parents(workspace, request['path']))
        if len(data) > max_bytes or total - before.get('size', 0) + len(data) > max_total_bytes or count + missing_parents + (before['kind'] == 'absent') > max_entries:
            raise ValueError('file or workspace budget exceeded')
    content_digest = _digest({key: request[key] for key in ('content', 'old_string', 'new_string') if key in request})
    return {'operation': request['operation'], 'path': request['path'],
            'request_digest': _digest(request), 'content_digest': content_digest,
            'before_digest': _digest(before), 'parents': _parents(workspace, request['path']),
            'max_bytes': max_bytes, 'max_total_bytes': max_total_bytes, 'max_entries': max_entries,
            'protected_diff_digest': _digest({'before': before, 'content_digest': content_digest}) if protected else ''}


def _parts(path):
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise PermissionError("invalid workspace-relative path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or parsed.as_posix() != path or parsed == PurePosixPath(".") or ".." in parsed.parts:
        raise PermissionError("path escapes workspace")
    return parsed.parts


def _parent_fd(workspace, path, create=False):
    parts = _parts(path)
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _read(workspace, path, max_bytes=MAX_FILE_BYTES):
    parent, name = _parent_fd(workspace, path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError("target is not a regular file")
            data = b""
            while chunk := os.read(fd, 1024 * 1024):
                data += chunk
                if len(data) > max_bytes:
                    raise ValueError("file exceeds helper limit")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _atomic_write(workspace, path, data, expected=None, mode=0o644):
    parent, name = _parent_fd(workspace, path, create=True)
    temp_name = None
    try:
        if expected is not None:
            if _digest(target_state(workspace, path)) != expected:
                raise RuntimeError("target changed concurrently")
        fd, temp_path = tempfile.mkstemp(prefix=".agent-write-", dir=f"/proc/self/fd/{parent}")
        temp_name = os.path.basename(temp_path)
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError('short write')
                remaining = remaining[written:]
            os.fsync(fd)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def handle(request, workspace="/workspace", *, grant=None):
    try:
        _validate_request(request)
        if not isinstance(grant, dict):
            raise PermissionError('file grant required')
        fresh = make_grant(request, workspace, max_bytes=grant.get('max_bytes'),
                           max_total_bytes=grant.get('max_total_bytes'), max_entries=grant.get('max_entries'),
                           protected=bool(grant.get('protected_diff_digest')))
        if fresh != grant:
            raise PermissionError('file grant does not match request or before-state')
        operation = request.get("operation")
        path = request.get("path")
        _parts(path)
        if operation == "read":
            content = _read(workspace, path, grant['max_bytes']).decode("utf-8")
            return {"success": True, "status": "success", "content": content}
        if operation == "fingerprint":
            data = _read(workspace, path, grant['max_bytes'])
            return {"success": True, "status": "success", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if operation == "write":
            data = request.get("content", "").encode("utf-8")
            before = target_state(workspace, path, grant['max_bytes'])
            _atomic_write(workspace, path, data, grant['before_digest'], before.get('mode', 0o644))
            return {"success": True, "status": "success", "sha256": hashlib.sha256(data).hexdigest()}
        if operation == "edit":
            data = _read(workspace, path, grant['max_bytes'])
            content = data.decode("utf-8")
            old = request.get("old_string", "")
            if not old or content.count(old) != 1:
                return {"success": False, "status": "runtime_error", "error": "old_string must match uniquely"}
            updated = content.replace(old, request.get("new_string", ""), 1).encode("utf-8")
            before = target_state(workspace, path, grant['max_bytes'])
            _atomic_write(workspace, path, updated, grant['before_digest'], before.get('mode', 0o644))
            return {"success": True, "status": "success", "sha256": hashlib.sha256(updated).hexdigest()}
        return {"success": False, "status": "runtime_error", "error": "unknown operation"}
    except (PermissionError, OSError) as error:
        if isinstance(error, PermissionError) or getattr(error, "errno", None) in (errno.ELOOP, errno.ENOTDIR):
            return {"success": False, "status": "blocked_by_sandbox", "error": str(error)}
        return {"success": False, "status": "runtime_error", "error": str(error)}
    except (UnicodeError, ValueError, RuntimeError) as error:
        return {"success": False, "status": "runtime_error", "error": str(error)}


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise SystemExit("request too large")
    envelope = json.loads(raw)
    print(json.dumps(handle(envelope.get('request'), grant=envelope.get('grant'))))


if __name__ == "__main__":
    main()
