"""
tools/verbs.py — bộ verb đóng cho structured shell tool.

Ý tưởng cốt lõi (để normalization/permission COVER ĐƯỢC):
    thay vì để model gen 1 chuỗi shell TỰ DO (không gian vô hạn, không thể
    cover), ta ép model chọn 1 `verb` trong enum ĐÓNG bên dưới. Khi đó không
    gian lệnh trở thành HỮU HẠN (~21 verb) -> allow/deny/ask trở nên O(1) và
    không thể bypass.

Mỗi VerbSpec gồm:
    name        - tên verb (giá trị enum model phải chọn)
    description - mô tả cho model biết verb này làm gì
    danger      - SAFE / MEDIUM / DANGER (để policy quyết định allow/deny/ask)
    params      - dict {tên field: mô tả}, dùng để validate args trong code
    render      - hàm args -> lệnh PowerShell canonical (chuẩn hoá)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Danger(Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    DANGER = "danger"


@dataclass
class VerbSpec:
    name: str
    description: str
    danger: Danger
    params: dict[str, str]
    render: Callable[[dict], str]


def _q(path: str) -> str:
    """Quote 1 path nếu có khoảng trắng (PowerShell dùng dấu nháy kép)."""
    if " " in path and not (path.startswith('"') and path.endswith('"')):
        return f'"{path}"'
    return path


# ---------------------------------------------------------------------------
# Git verbs
# ---------------------------------------------------------------------------
def _git_status(a: dict) -> str:
    return "git status --porcelain"


def _git_diff(a: dict) -> str:
    return "git diff"


def _git_log(a: dict) -> str:
    n = int(a.get("n", 10))
    return f"git log --oneline -n {n}"


def _git_add(a: dict) -> str:
    paths = " ".join(_q(p) for p in a.get("paths", []))
    return f"git add {paths}".strip()


def _git_commit(a: dict) -> str:
    msg = a["message"].replace('"', '\\"')
    return f'git commit -m "{msg}"'


def _git_push(a: dict) -> str:
    return "git push"


def _git_pull(a: dict) -> str:
    return "git pull"


def _git_checkout(a: dict) -> str:
    return f"git checkout {a['branch']}"


def _git_branch(a: dict) -> str:
    return "git branch"


def _git_reset(a: dict) -> str:
    return "git reset " + a.get("args", "")


# ---------------------------------------------------------------------------
# Process verbs
# ---------------------------------------------------------------------------
def _list_process(a: dict) -> str:
    return "Get-Process"


def _kill_process(a: dict) -> str:
    return f"Stop-Process -Name {a['name']} -Force"


# ---------------------------------------------------------------------------
# Package / test / script verbs
# ---------------------------------------------------------------------------
def _install_package(a: dict) -> str:
    return f"pip install {a['package']}"


def _run_test(a: dict) -> str:
    return a.get("command", "pytest")


def _run_python(a: dict) -> str:
    script = _q(a["script"])
    args = a.get("args", "")
    return f"python {script} {args}".strip()


# ---------------------------------------------------------------------------
# Filesystem verbs (bổ sung cho read/write/edit tool riêng)
# ---------------------------------------------------------------------------
def _list_files(a: dict) -> str:
    path = _q(a.get("path", "."))
    recursive = " -Recurse" if a.get("recursive") else ""
    return f"Get-ChildItem {path}{recursive}"


def _search_content(a: dict) -> str:
    path = _q(a.get("path", "."))
    return f"Select-String -Path {path} -Pattern {a['pattern']!r}"


def _make_dir(a: dict) -> str:
    return f"New-Item -ItemType Directory -Force {_q(a['path'])}"


def _remove_path(a: dict) -> str:
    return f"Remove-Item {_q(a['path'])} -Recurse -Force"


def _print_env(a: dict) -> str:
    return "Get-ChildItem Env:"


# ---------------------------------------------------------------------------
# Network verbs
# ---------------------------------------------------------------------------
def _fetch_url(a: dict) -> str:
    return f"Invoke-WebRequest -Uri {a['url']}"


# ---------------------------------------------------------------------------
# Bảng VERBS (enum đóng)
# ---------------------------------------------------------------------------
VERBS: dict[str, VerbSpec] = {
    "git_status": VerbSpec(
        "git_status", "Show working tree status (porcelain).", Danger.SAFE,
        {}, _git_status,
    ),
    "git_diff": VerbSpec(
        "git_diff", "Show staged and unstaged changes.", Danger.SAFE,
        {}, _git_diff,
    ),
    "git_log": VerbSpec(
        "git_log", "Show recent commit history.", Danger.SAFE,
        {"n": "number of commits (default 10)"}, _git_log,
    ),
    "git_add": VerbSpec(
        "git_add", "Stage specific files by name.", Danger.SAFE,
        {"paths": "list of file paths to stage"}, _git_add,
    ),
    "git_commit": VerbSpec(
        "git_commit", "Create a new commit with a message.", Danger.MEDIUM,
        {"message": "commit message"}, _git_commit,
    ),
    "git_push": VerbSpec(
        "git_push", "Push commits to the remote.", Danger.MEDIUM,
        {}, _git_push,
    ),
    "git_pull": VerbSpec(
        "git_pull", "Pull changes from the remote.", Danger.MEDIUM,
        {}, _git_pull,
    ),
    "git_checkout": VerbSpec(
        "git_checkout", "Switch to a branch.", Danger.MEDIUM,
        {"branch": "branch name"}, _git_checkout,
    ),
    "git_branch": VerbSpec(
        "git_branch", "List branches.", Danger.SAFE,
        {}, _git_branch,
    ),
    "git_reset": VerbSpec(
        "git_reset", "Reset the index/working tree (DESTRUCTIVE).", Danger.DANGER,
        {"args": "reset arguments (e.g. --hard HEAD~1)"}, _git_reset,
    ),

    "list_process": VerbSpec(
        "list_process", "List running processes.", Danger.SAFE,
        {}, _list_process,
    ),
    "kill_process": VerbSpec(
        "kill_process", "Terminate a process by name (DESTRUCTIVE).", Danger.DANGER,
        {"name": "process name"}, _kill_process,
    ),

    "install_package": VerbSpec(
        "install_package", "Install a Python package via pip.", Danger.MEDIUM,
        {"package": "package name"}, _install_package,
    ),
    "run_test": VerbSpec(
        "run_test", "Run the test suite.", Danger.MEDIUM,
        {"command": "test command (default pytest)"}, _run_test,
    ),
    "run_python": VerbSpec(
        "run_python", "Run a Python script.", Danger.MEDIUM,
        {"script": "script path", "args": "cli arguments"}, _run_python,
    ),

    "list_files": VerbSpec(
        "list_files", "List directory contents.", Danger.SAFE,
        {"path": "directory path", "recursive": "bool, recurse subdirs"}, _list_files,
    ),
    "search_content": VerbSpec(
        "search_content", "Search text in files.", Danger.SAFE,
        {"path": "path to search", "pattern": "regex pattern"}, _search_content,
    ),
    "make_dir": VerbSpec(
        "make_dir", "Create a directory.", Danger.SAFE,
        {"path": "directory path"}, _make_dir,
    ),
    "remove_path": VerbSpec(
        "remove_path", "Delete a file/directory recursively (DESTRUCTIVE).", Danger.DANGER,
        {"path": "path to delete"}, _remove_path,
    ),
    "print_env": VerbSpec(
        "print_env", "List environment variables.", Danger.SAFE,
        {}, _print_env,
    ),

    "fetch_url": VerbSpec(
        "fetch_url", "Download content from a URL.", Danger.MEDIUM,
        {"url": "the URL"}, _fetch_url,
    ),
}


def get_verb(name: str) -> VerbSpec | None:
    return VERBS.get(name)


def validate_args(verb: VerbSpec, args: dict[str, Any]) -> str | None:
    """Kiểm tra args có đủ field bắt buộc không. Trả None nếu hợp lệ."""
    for field in verb.params:
        if field not in args:
            return f"Missing required argument '{field}' for verb '{verb.name}'"
    return None


def build_tool_schema(tool_name: str = "shell") -> dict:
    """Sinh JSON schema cho structured shell tool (verb là enum đóng)."""
    verb_lines = []
    for name, spec in VERBS.items():
        params = ", ".join(f"{k}: {v}" for k, v in spec.params.items()) or "no args"
        verb_lines.append(f"- {name} [{spec.danger.value}] ({params}) - {spec.description}")

    description = (
        "Run a shell operation on Windows (PowerShell). Choose exactly one "
        "`verb` from the list below — you MUST NOT invent a command string. "
        "Fill `args` with the fields required by that verb.\n\n"
        "Available verbs:\n" + "\n".join(verb_lines)
    )

    return {
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "enum": list(VERBS.keys()),
                "description": "The operation to perform.",
            },
            "args": {
                "type": "object",
                "description": "Arguments for the chosen verb (see the verb list for required fields).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (default: current directory).",
            },
        },
        "required": ["verb"],
    }
