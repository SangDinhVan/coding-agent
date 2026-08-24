"""
windows_shell_prompt.py — builder prompt cho shell tool trên Windows.

Bản port sang Python của `a.ts` (BashTool prompt của Claude Code), nhưng thu
hẹp phạm vi: CHỈ rào những quy tắc liên quan tới việc model GEN RA lệnh
PowerShell / cmd.exe.

Khác với a.ts ở 2 điểm cốt lõi:
1. Target là Windows shell (PowerShell + cmd.exe), không phải bash/zsh.
2. Đây là rào MỀM (soft guardrail): trả về một chuỗi TEXT để nhét vào prompt,
   KHÔNG phải cơ chế chặn lệnh thật sự. Rào cứng (chặn ở tầng OS) nằm ở nơi
   khác (sandbox / permission system).

File này CHỈ là prompt-builder độc lập, chưa nối vào agent/terminal tool.
"""

from __future__ import annotations

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Cấu hình timeout — có thể override qua env, mặc định giống tool hiện tại.
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT_MS = 60_000   # 60 giây
MAX_TIMEOUT_MS = 600_000      # 10 phút

# Tên tool shell dùng trong prompt (để prompt không hardcode nhầm tên).
SHELL_TOOL_NAME = "shell"


def _is_env_truthy(name: str) -> bool:
    """Bản thay thế tối giản cho isEnvTruthy() trong a.ts."""
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _feature(name: str) -> bool:
    """Bản thay thế tối giản cho feature() từ 'bun:bundle' — chỉ check env flag."""
    return _is_env_truthy(f"FEATURE_{name}")


def get_default_timeout_ms() -> int:
    return int(os.environ.get("SHELL_DEFAULT_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))


def get_max_timeout_ms() -> int:
    return int(os.environ.get("SHELL_MAX_TIMEOUT_MS", MAX_TIMEOUT_MS))


# ---------------------------------------------------------------------------
# Shell detection
# ---------------------------------------------------------------------------
def detect_shell() -> str:
    """
    Tự detect shell mục tiêu trên Windows. Trả về 'powershell' hoặc 'cmd'.

    Heuristic:
    - Có PSModulePath (biến riêng của PowerShell) -> powershell.
    - Có COMSPEC (trỏ tới cmd.exe) và KHÔNG có dấu hiệu PowerShell -> cmd.
    - Mặc định fallback: powershell (phổ biến & an toàn hơn trên Windows 10+).
    """
    if os.environ.get("PSModulePath"):
        return "powershell"
    if os.environ.get("COMSPEC"):
        return "cmd"
    # Trên Windows hiện đại PowerShell là mặc định hợp lý.
    return "powershell"


# ---------------------------------------------------------------------------
# Các mảnh rào nhỏ (giữ nguyên tinh thần từ a.ts, dịch sang Windows shell)
# ---------------------------------------------------------------------------
def get_background_usage_note() -> Optional[str]:
    """Hướng dẫn chạy nền thay vì để process treo. Có thể tắt qua env."""
    if _is_env_truthy("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"):
        return None
    return (
        "You can run a command in the background and get notified when it "
        "finishes. Do NOT append `&` at the end of a command for this purpose "
        "(in PowerShell `&` is the call operator, not a background marker). "
        "You do not need to poll the output right away - you will be notified "
        "when it completes."
    )


def get_git_instructions() -> str:
    """
    Rào git: chỉ cho commit khi được yêu cầu rõ ràng, không chạy lệnh phá huỷ,
    không skip hooks, ưu tiên tạo commit mới thay vì amend.
    """
    return f"""# Git operations

Git Safety Protocol:
- NEVER update the git config
- NEVER run destructive git commands (push --force, reset --hard, checkout ., restore ., clean -f, branch -D) unless the user explicitly requests them
- NEVER skip hooks (--no-verify, --no-gpg-sign) unless the user explicitly requests it
- NEVER force push to main/master; warn the user if they request it
- CRITICAL: always create NEW commits rather than amending, unless the user explicitly asks to amend. If a pre-commit hook fails, the commit did NOT happen - so --amend would modify the PREVIOUS commit and may destroy work.
- When staging, prefer adding specific files by name instead of "git add -A" or "git add ."
- NEVER commit changes unless the user explicitly asks you to

Use the {SHELL_TOOL_NAME} tool for git commands. Never use the -uall flag (memory issues on large repos). Never use git commands with the -i flag (interactive input is not supported)."""


def get_shell_specific_section() -> str:
    """Cú pháp riêng của PowerShell vs cmd.exe — để model gen đúng dialect."""
    shell = detect_shell()

    if shell == "powershell":
        return """# PowerShell syntax

- Use PowerShell syntax: variables are `$var`, not `%var%`
- Prefer PowerShell cmdlets over legacy aliases where they differ (e.g. `Get-ChildItem` over `dir`, `Select-String` over `findstr`)
- Paths: PowerShell accepts both `\\` and `/`; quote any path containing spaces with double quotes
- `$?` / `$LASTEXITCODE` hold the last command status / exit code
- For a background job use `Start-Process` / `Start-Job`, NOT a trailing `&`
- `;` separates statements that run regardless of prior failure; use `-and` / `if` for conditional chaining"""

    return """# cmd.exe syntax

- Use cmd.exe syntax: variables are `%var%`, not `$var`
- Use `%ERRORLEVEL%` for the last command's exit code
- Quote any path containing spaces with double quotes; prefer `\\` path separators
- `&` chains two commands sequentially; `&&` runs the next only if the previous succeeded; `||` runs the next only if the previous failed
- `;` is NOT a command separator in cmd.exe"""


def get_sleep_avoidance_section() -> str:
    return """- Do not sleep between commands that can run immediately - just run them
- Do not retry failing commands in a sleep loop - diagnose the root cause
- If waiting for a background task you started, you will be notified when it completes - do not poll
- If you must delay (rate limiting, deliberate pacing), keep it short (1-5 seconds) to avoid blocking the user"""


def get_general_instruction_items() -> list[str]:
    """Các quy tắc chung khi gen command (chaining, path, cwd, timeout...)."""
    return [
        "If commands are independent and can run in parallel, make multiple tool calls in a single message.",
        "If commands depend on each other and must run sequentially, chain them with `&&` (both PowerShell and cmd support `&&`).",
        "Use `;` only when you need to run commands sequentially but do not care if earlier commands fail.",
        "Do NOT use newlines to separate commands (newlines are ok inside quoted strings).",
        "Always quote file paths that contain spaces with double quotes.",
        "Prefer absolute paths and avoid relying on `cd`; you may use `cd` only if the user explicitly requests it.",
        "If your command will create new directories or files, first verify the parent directory exists and is the correct location.",
        f"You may specify an optional timeout in milliseconds (up to {get_max_timeout_ms()}ms / {get_max_timeout_ms() // 60000} minutes). "
        f"By default, your command will timeout after {get_default_timeout_ms()}ms ({get_default_timeout_ms() // 60000} minutes).",
    ]


# ---------------------------------------------------------------------------
# Entry point chính — mirror getSimplePrompt() trong a.ts
# ---------------------------------------------------------------------------
def get_simple_prompt() -> str:
    """Trả về toàn bộ prompt rào cho shell tool, đã lắp ghép các mảnh nhỏ."""
    shell = detect_shell()

    # Mục chung: mỗi item là một bullet riêng.
    general_bullets = "\n".join(
        f"- {item}" for item in get_general_instruction_items()
    )

    background_note = get_background_usage_note()
    background_section = (
        f"- {background_note}" if background_note is not None else ""
    )

    # Mục git + sleep: là các khối đã có header/bullet riêng, không gắn thêm '- '.
    git_section = get_git_instructions().rstrip()
    sleep_section = get_sleep_avoidance_section().rstrip()

    sections = [
        f"Executes a given {shell} command and returns its output.",
        "",
        "The working directory persists between commands, but shell state does not. "
        "The shell environment is initialized from the user's profile.",
        "",
        get_shell_specific_section(),
        "",
        "# Instructions",
        general_bullets,
    ]

    if background_section:
        sections.append(background_section)

    sections.extend([
        "",
        "For git commands:",
        git_section,
        "",
        "Avoid unnecessary sleep:",
        sleep_section,
    ])

    return "\n".join(sections)


if __name__ == "__main__":
    # Chạy trực tiếp để xem thử prompt sinh ra (không cần nối vào agent).
    print(f"Detected shell: {detect_shell()}")
    print("=" * 60)
    print(get_simple_prompt())
