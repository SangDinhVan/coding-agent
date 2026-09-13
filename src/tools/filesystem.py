import hashlib
from tools.base import BaseTool, ToolResult
from runtime.models import ReplayPolicy

COMPACT_PREVIEW_LINES = 50


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unbound(name):
    return ToolResult(f"SandboxUnavailable: {name}", "Sandbox is unavailable; host filesystem execution is forbidden", False)


class _SandboxFileTool(BaseTool):
    def __init__(self, sandbox=None):
        self.sandbox = sandbox

    def recovery_fingerprint(self, metadata):
        return self.sandbox.fingerprint(metadata["path"]) if self.sandbox else None

    def _call(self, request):
        if self.sandbox is None:
            return None, _unbound(self.name)
        data = self.sandbox.fs_call(request)
        if not data.get("success"):
            message = data.get("error", "sandbox filesystem operation failed")
            return data, ToolResult(message, f"{self.name} failed: {message}", False, metadata={"sandbox_status": data.get("status", "unknown")})
        return data, None


class ReadTool(_SandboxFileTool):
    replay_policy = ReplayPolicy.REPLAY_SAFE
    name = "read"
    description = "Đọc UTF-8 file trong isolated sandbox workspace. Chỉ nhận path tương đối."
    parameters = {"type": "object", "properties": {"path": {"type": "string", "description": "Workspace-relative path."}}, "required": ["path"]}

    def execute(self, path: str, **kwargs):
        data, error = self._call({"operation": "read", "path": path})
        if error: return error
        content = data["content"]
        lines = content.splitlines()
        numbered = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))
        compact = numbered if len(lines) <= COMPACT_PREVIEW_LINES else numbered.splitlines()[:COMPACT_PREVIEW_LINES]
        if isinstance(compact, list):
            compact = "\n".join(compact) + f"\n... ({len(lines) - COMPACT_PREVIEW_LINES} more lines)"
        return ToolResult(numbered, compact, True, metadata={"sandbox_status": data["status"]})


class WriteTool(_SandboxFileTool):
    replay_policy = ReplayPolicy.RECONCILABLE
    name = "write"
    description = "Ghi đè hoặc tạo UTF-8 file trong isolated sandbox workspace."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}

    def recovery_metadata(self, path: str, content: str, **kwargs):
        before = self.sandbox.fingerprint(path) if self.sandbox else None
        return {"path": path, "before_hash": before or "__missing__", "expected_after_hash": _hash_bytes(content.encode())}

    def execute(self, path: str, content: str, **kwargs):
        data, error = self._call({"operation": "write", "path": path, "content": content})
        if error: return error
        return ToolResult(f"Wrote {path}", f"Successfully wrote {path}", True, metadata={"sandbox_status": data["status"], "sha256": data.get("sha256")})


class EditTool(_SandboxFileTool):
    replay_policy = ReplayPolicy.RECONCILABLE
    name = "edit"
    description = "Thay unique old_string trong UTF-8 file thuộc isolated sandbox workspace."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}

    def recovery_metadata(self, path: str, old_string: str, new_string: str, **kwargs):
        return {"path": path, "before_hash": self.sandbox.fingerprint(path) if self.sandbox else None}

    def execute(self, path: str, old_string: str, new_string: str, **kwargs):
        data, error = self._call({"operation": "edit", "path": path, "old_string": old_string, "new_string": new_string})
        if error: return error
        return ToolResult(f"Edited {path}", f"Successfully edited {path}", True, metadata={"sandbox_status": data["status"], "sha256": data.get("sha256")})
