#!/usr/bin/env python3
"""Bounded shell supervisor executed inside the sandbox namespace."""
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath


def _cwd(workspace, value):
    value = value or "."
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise PermissionError("cwd escapes workspace")
    root = Path(workspace).resolve()
    candidate = (root / path.as_posix()).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_dir():
        raise PermissionError("cwd escapes workspace or is not a directory")
    return candidate


def execute(request, workspace="/workspace", output_limit=10 * 1024 * 1024):
    try:
        cwd = _cwd(workspace, request.get("cwd", "."))
    except (PermissionError, OSError) as error:
        return {"success": False, "status": "blocked_by_sandbox", "stdout": "", "stderr": str(error), "exit_code": None, "truncated": False}
    timeout = float(request.get("timeout_seconds", 60))
    if timeout <= 0 or timeout > 600:
        return {"success": False, "status": "runtime_error", "stdout": "", "stderr": "invalid timeout", "exit_code": None, "truncated": False}
    process = subprocess.Popen(
        ["/bin/bash", "-lc", request.get("command", "")], cwd=cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    truncated = False
    deadline = time.monotonic() + timeout
    timed_out = False
    while selector.get_map() or process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            break
        for key, _ in selector.select(min(remaining, 0.05)):
            data = os.read(key.fileobj.fileno(), 65536)
            if not data:
                selector.unregister(key.fileobj)
                continue
            allowed = max(0, output_limit - total)
            chunks[key.data].extend(data[:allowed])
            total += min(len(data), allowed)
            truncated = truncated or len(data) > allowed
    process.wait()
    return_code = process.returncode
    selector.close()
    process.stdout.close()
    process.stderr.close()
    return {
        "success": not timed_out and return_code == 0,
        "status": "timed_out" if timed_out else ("success" if return_code == 0 else "runtime_error"),
        "stdout": chunks["stdout"].decode("utf-8", "replace"),
        "stderr": chunks["stderr"].decode("utf-8", "replace"),
        "exit_code": return_code,
        "truncated": truncated,
    }


def main():
    print(json.dumps(execute(json.load(sys.stdin))))


if __name__ == "__main__":
    main()
