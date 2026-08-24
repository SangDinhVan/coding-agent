"""
memory/manager.py — MemoryManager: đọc/ghi USER.md + PROJECT.md (nhớ xuyên session).

Chạy KHÔNG PHẢI sau mọi message — agent/loop.py quyết định KHI NÀO gọi
update() (task hoàn thành / session sắp đóng / có decision-constraint quan
trọng). MemoryManager chỉ lo việc đọc/ghi file + gọi LLM sinh diff.

update() chỉ APPEND, không tự ý xóa nội dung cũ. Việc gộp fact trùng lặp khi
file quá dài (Memory Consolidation) là 1 bước riêng, chạy khi cần — không
nằm trong update() này để tránh gọi LLM tốn kém mỗi lần cập nhật nhỏ.
"""

import json
from pathlib import Path
from typing import Optional

from model import llm

DEFAULT_USER_MD_PATH = "src/memory/private/USER.md"
DEFAULT_PROJECT_MD_PATH = "src/memory/private/PROJECT.md"

USER_MD_TEMPLATE = """# USER.md

## Preferences
(chưa có dữ liệu — agent sẽ tự điền dần qua các session)
"""

PROJECT_MD_TEMPLATE = """# PROJECT.md

## Overview
(chưa có dữ liệu)

## Architecture
(chưa có dữ liệu)

## Important Decisions
(chưa có dữ liệu)

## Hard Constraints
(chưa có dữ liệu)

## Coding Conventions
(chưa có dữ liệu)

## Known Problems
(chưa có dữ liệu)

## Failed Approaches
(chưa có dữ liệu — mục này BẮT BUỘC giữ lại mọi giải pháp đã thử mà thất bại,
để agent không lặp lại sai lầm cũ)
"""

DIFF_PROMPT_TEMPLATE = """\
Bạn đang giúp duy trì 2 file memory bền vững cho 1 coding agent cá nhân:

USER.md — preference cá nhân của người dùng (ngôn ngữ lập trình ưu tiên, \
phong cách code, thói quen làm việc...)

PROJECT.md — facts về project hiện tại (Overview, Architecture, Important \
Decisions, Hard Constraints, Coding Conventions, Known Problems, Failed \
Approaches — Failed Approaches BẮT BUỘC giữ lại để agent không lặp lại giải \
pháp đã thất bại)

--- USER.md hiện tại ---
{user_md}

--- PROJECT.md hiện tại ---
{project_md}

--- Đoạn hội thoại gần đây ---
{recent_conversation}

Dựa vào đoạn hội thoại trên, xác định có thông tin gì MỚI hoặc THAY ĐỔI cần \
cập nhật vào 2 file trên không. CHỈ trả về JSON đúng format sau, KHÔNG thêm \
text nào khác, KHÔNG dùng markdown code fence:

{{
  "user_md_append": "<đoạn text cần thêm vào USER.md, rỗng nếu không có gì>",
  "project_md_append": "<đoạn text cần thêm vào PROJECT.md, rỗng nếu không có gì>"
}}

Chỉ thêm thông tin THỰC SỰ quan trọng và bền vững (không phải chi tiết vụn \
vặt của 1 lần trò chuyện). Nếu không có gì đáng nhớ, trả 2 field rỗng."""


class MemoryManager:
    def __init__(
        self,
        user_md_path: str = DEFAULT_USER_MD_PATH,
        project_md_path: str = DEFAULT_PROJECT_MD_PATH,
        model: Optional[str] = None,
    ):
        self.user_md_path = Path(user_md_path)
        self.project_md_path = Path(project_md_path)
        self.model = model
        self._ensure_files_exist()

    def _ensure_files_exist(self):
        self.user_md_path.parent.mkdir(parents=True, exist_ok=True)
        self.project_md_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.user_md_path.exists():
            self.user_md_path.write_text(USER_MD_TEMPLATE, encoding="utf-8")
        if not self.project_md_path.exists():
            self.project_md_path.write_text(PROJECT_MD_TEMPLATE, encoding="utf-8")

    def read(self) -> tuple[str, str]:
        """Đọc nguyên 2 file — dùng để nhét thẳng vào system prompt mỗi turn."""
        return (
            self.user_md_path.read_text(encoding="utf-8"),
            self.project_md_path.read_text(encoding="utf-8"),
        )

    def update(self, recent_conversation: str) -> dict:
        """
        Gọi 1 LLM call nhỏ để xác định có gì cần thêm vào USER.md/PROJECT.md
        không, patch trực tiếp vào file nếu có. Trả về dict diff đã áp dụng
        (để log/debug), không raise nếu model trả sai format — chỉ bỏ qua
        lần update này, không làm hỏng file.
        """
        user_md, project_md = self.read()

        prompt = DIFF_PROMPT_TEMPLATE.format(
            user_md=user_md,
            project_md=project_md,
            recent_conversation=recent_conversation,
        )
        raw_response = llm.complete_text(prompt, model=self.model)

        try:
            diff = json.loads(raw_response)
        except json.JSONDecodeError:
            return {"user_md_append": "", "project_md_append": "", "error": "invalid_json"}

        user_append = (diff.get("user_md_append") or "").strip()
        project_append = (diff.get("project_md_append") or "").strip()

        if user_append:
            with self.user_md_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{user_append}\n")

        if project_append:
            with self.project_md_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{project_append}\n")

        return {"user_md_append": user_append, "project_md_append": project_append}