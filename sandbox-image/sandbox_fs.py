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


def _parts(path):
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise PermissionError("invalid workspace-relative path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or parsed.as_posix() != path or parsed == PurePosixPath(".") or ".." in parsed.parts:
        raise PermissionError("path escapes workspace")
    return parsed.parts


def _parent_fd(workspace, path, create=False):
    parts = _parts(path)
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
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


def _read(workspace, path):
    parent, name = _parent_fd(workspace, path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("target is not a regular file")
            data = b""
            while chunk := os.read(fd, 1024 * 1024):
                data += chunk
                if len(data) > MAX_REQUEST_BYTES:
                    raise ValueError("file exceeds helper limit")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _atomic_write(workspace, path, data, expected=None):
    parent, name = _parent_fd(workspace, path, create=True)
    temp_name = None
    try:
        if expected is not None:
            current = _read(workspace, path)
            if hashlib.sha256(current).hexdigest() != expected:
                raise RuntimeError("target changed concurrently")
        fd, temp_path = tempfile.mkstemp(prefix=".agent-write-", dir=f"/proc/self/fd/{parent}")
        temp_name = os.path.basename(temp_path)
        try:
            os.write(fd, data)
            os.fsync(fd)
            os.fchmod(fd, 0o644)
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


def handle(request, workspace="/workspace"):
    try:
        operation = request.get("operation")
        path = request.get("path")
        _parts(path)
        if operation == "read":
            content = _read(workspace, path).decode("utf-8")
            return {"success": True, "status": "success", "content": content}
        if operation == "fingerprint":
            data = _read(workspace, path)
            return {"success": True, "status": "success", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if operation == "write":
            data = request.get("content", "").encode("utf-8")
            _atomic_write(workspace, path, data)
            return {"success": True, "status": "success", "sha256": hashlib.sha256(data).hexdigest()}
        if operation == "edit":
            data = _read(workspace, path)
            content = data.decode("utf-8")
            old = request.get("old_string", "")
            if not old or content.count(old) != 1:
                return {"success": False, "status": "runtime_error", "error": "old_string must match uniquely"}
            updated = content.replace(old, request.get("new_string", ""), 1).encode("utf-8")
            _atomic_write(workspace, path, updated, hashlib.sha256(data).hexdigest())
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
    print(json.dumps(handle(json.loads(raw))))


if __name__ == "__main__":
    main()
