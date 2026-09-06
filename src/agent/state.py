"""Live workspace projection; durable runtime state lives in runtime models."""

import subprocess
from dataclasses import dataclass


@dataclass
class WorkspaceState:
    """Read-only environment snapshot rebuilt from the actual workspace."""

    cwd: str = "."

    def snapshot(self) -> dict:
        return {
            "cwd": self.cwd,
            "git_head": self._get_git_head(),
            "modified_files": self._get_git_modified_files(),
        }

    def _get_git_head(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else "(not a git repo)"
        except Exception:
            return "(git unavailable)"

    def _get_git_modified_files(self) -> list[str]:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return []
            return [line[3:] for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def render(self) -> str:
        snap = self.snapshot()
        return (
            f"cwd: {snap['cwd']}\n"
            f"git branch: {snap['git_head']}\n"
            f"modified files: {', '.join(snap['modified_files']) or '(none)'}"
        )


ToolState = WorkspaceState
