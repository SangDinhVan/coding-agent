"""
terminal.py — 1 tool duy nhất: bash, chạy shell command và trả stdout/stderr/exit code.

Lưu ý quan trọng (ghi rõ để nhớ khi làm phần permission/sandbox sau này —
phần này CHƯA làm, tool hiện tại chạy trực tiếp trên máy host, không sandbox):
- timeout mặc định để tránh 1 lệnh treo làm agent loop kẹt vĩnh viễn
- output terminal thường rất dài (build log, test log...) -> compact phải
  cắt mạnh, đúng nguyên tắc "Tool output phải nén mạnh" trong plan
"""

import subprocess
from tools.base import BaseTool, ToolResult
from runtime.models import ReplayPolicy

DEFAULT_TIMEOUT_SECONDS = 60

# Compact chỉ giữ lại N ký tự cuối cùng của output — thường lỗi/kết quả
# quan trọng nhất nằm ở cuối log (ví dụ traceback, test summary).
COMPACT_MAX_CHARS = 2000


class BashTool(BaseTool):
    replay_policy = ReplayPolicy.MANUAL
    def __init__(self):
        self.name = "bash"
        self.description = (
            "Chạy 1 shell command và trả về stdout, stderr, exit code. "
            f"Command sẽ bị kill nếu chạy quá {DEFAULT_TIMEOUT_SECONDS} giây."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command cần chạy.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Thư mục làm việc để chạy command (mặc định: thư mục hiện tại của process).",
                },
            },
            "required": ["command"],
        }

    def execute(self, command: str, cwd: str = None, **kwargs) -> ToolResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                errors="replace"

            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                raw=f"Command timed out after {DEFAULT_TIMEOUT_SECONDS}s: {command}",
                compact=(
                    f"Command timed out after {DEFAULT_TIMEOUT_SECONDS}s. "
                    f"It may still be running in the background — consider a "
                    f"shorter-running command or check process state."
                ),
                success=False,
            )
        except OSError as e:
            return ToolResult(
                raw=f"OSError running command: {command}\n{e}",
                compact=f"Failed to run command: {e}",
                success=False,
            )

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        exit_code = result.returncode
        success = exit_code == 0

        raw = (
            f"$ {command}\n"
            f"exit code: {exit_code}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )

        combined = (stdout + stderr).strip()
        if len(combined) <= COMPACT_MAX_CHARS:
            output_preview = combined if combined else "(no output)"
        else:
            # Giữ lại phần CUỐI vì lỗi/kết quả quan trọng nhất thường ở cuối log.
            truncated_chars = len(combined) - COMPACT_MAX_CHARS
            output_preview = (
                f"...({truncated_chars} chars truncated)...\n"
                f"{combined[-COMPACT_MAX_CHARS:]}"
            )

        compact = f"$ {command}\nexit code: {exit_code}\n{output_preview}"

        return ToolResult(raw=raw, compact=compact, success=success)   