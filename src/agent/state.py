"""
agent/state.py — TurnState, PlanState, ToolState.

3 state này KHÔNG nằm trong `messages` gửi model, nên KHÔNG bị Compactor đụng
vào — dù compact bao nhiêu lần, 3 state này vẫn giữ nguyên. agent/loop.py sẽ
render text của cả 3 vào system message mỗi turn, tách biệt hoàn toàn khỏi
phần lịch sử hội thoại nằm trong EventStore.
"""

import subprocess
from dataclasses import dataclass, field


@dataclass
class PlanState:
    """
    Danh sách step của task hiện tại, có status riêng — độc lập với chat
    history. Đây là lý do PlanState phải tách khỏi `messages`: nếu nhét plan
    vào 1 message thường, Compactor có thể tóm tắt mất chi tiết step nào
    done/pending, khiến agent quên progress giữa chừng.
    """

    steps: list[dict] = field(default_factory=list)

    def add_step(self, task: str) -> str:
        step_id = f"step_{len(self.steps) + 1}"
        self.steps.append({"id": step_id, "task": task, "status": "pending"})
        return step_id

    def update_status(self, step_id: str, status: str):
        for step in self.steps:
            if step["id"] == step_id:
                step["status"] = status
                return
        raise ValueError(f"Step not found: {step_id}")

    def render(self) -> str:
        if not self.steps:
            return "(no plan yet)"
        return "\n".join(f"[{s['status']}] {s['id']}: {s['task']}" for s in self.steps)


@dataclass
class TurnState:
    """
    State của 1 turn hiện tại (1 user request, có thể chạy nhiều tool call
    bên trong). Khởi tạo mới mỗi khi có user request mới — khác với PlanState
    có thể tồn tại xuyên nhiều turn nếu task lớn (agent/loop.py quyết định
    khi nào tạo PlanState mới).
    """

    goal: str
    status: str = "running"  # running | done | failed
    files_touched: list[str] = field(default_factory=list)
    last_test_result: str = ""
    current_problem: str = ""

    def mark_file_touched(self, path: str):
        if path not in self.files_touched:
            self.files_touched.append(path)

    def render(self) -> str:
        return (
            f"Goal: {self.goal}\n"
            f"Status: {self.status}\n"
            f"Files touched this turn: {', '.join(self.files_touched) or '(none)'}\n"
            f"Last test result: {self.last_test_result or '(none)'}\n"
            f"Current problem: {self.current_problem or '(none)'}"
        )


@dataclass
class ToolState:
    """
    State cấp giây/phút — cwd hiện tại, git HEAD, file đã sửa (theo git status).
    Đọc trực tiếp từ hệ thống mỗi lần render() thay vì tự track thủ công, để
    luôn đúng thực tế dù agent hay user tự đổi branch/cwd bên ngoài vòng lặp.

    V1: KHÔNG track process đang chạy (pid, command) — thêm sau khi thật sự
    cần (ví dụ khi có tool chạy background process dài hạn).
    """

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