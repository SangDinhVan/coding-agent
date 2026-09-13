"""Sandbox-only shell tool. Host shell execution is intentionally forbidden."""
from sandbox.models import ExecRequest, SandboxPath
from tools.base import BaseTool, ToolResult
from runtime.models import ReplayPolicy

DEFAULT_TIMEOUT_SECONDS = 60
COMPACT_MAX_CHARS = 2000


class BashTool(BaseTool):
    replay_policy = ReplayPolicy.MANUAL
    name = "bash"

    def __init__(self, sandbox=None):
        self.sandbox = sandbox
        self.description = f"Chạy shell trong isolated offline sandbox; timeout mặc định {DEFAULT_TIMEOUT_SECONDS}s."
        self.parameters = {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string", "description": "Workspace-relative directory."}}, "required": ["command"]}

    def execute(self, command: str, cwd: str = ".", **kwargs):
        if self.sandbox is None:
            return ToolResult("SandboxUnavailable", "Sandbox is unavailable; host shell execution is forbidden", False)
        relative_cwd = "." if cwd in (None, "", ".") else cwd
        try:
            request = ExecRequest("tool", command, SandboxPath(relative_cwd), DEFAULT_TIMEOUT_SECONDS)
        except ValueError as error:
            return ToolResult(str(error), f"Invalid sandbox command: {error}", False)
        result = self.sandbox.exec(request)
        combined = (result.stdout + result.stderr).strip()
        preview = combined if len(combined) <= COMPACT_MAX_CHARS else f"...({len(combined)-COMPACT_MAX_CHARS} chars truncated)...\n{combined[-COMPACT_MAX_CHARS:]}"
        metadata = {"sandbox_session_id": result.sandbox_session_id, "container_id": result.container_id, "image_digest": result.image_digest, "violation_status": result.violation_status.value, "truncated": result.truncated}
        raw = f"$ {command}\nexit code: {result.exit_code}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        return ToolResult(raw, f"$ {command}\nexit code: {result.exit_code}\n{preview or '(no output)'}", result.success, result.exit_code, metadata)
