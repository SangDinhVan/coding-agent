"""MemoryManager: đọc và cập nhật PROJECT.md xuyên các session."""

import json
from pathlib import Path
from typing import Optional

from model import llm

DEFAULT_PROJECT_MD_PATH = "src/memory/private/PROJECT.md"

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
Bạn đang giúp duy trì PROJECT.md, file memory bền vững cho coding agent.
File chứa facts về project hiện tại: Overview, Architecture, Important Decisions,
Hard Constraints, Coding Conventions, Known Problems và Failed Approaches.

--- PROJECT.md hiện tại ---
{project_md}

--- Đoạn hội thoại gần đây ---
{recent_conversation}

Xác định thông tin MỚI hoặc THAY ĐỔI thực sự quan trọng và bền vững cần thêm.
CHỈ trả JSON đúng format sau, không thêm text hoặc markdown code fence:

{{"project_md_append": "<đoạn text cần thêm, rỗng nếu không có gì>"}}
"""


class MemoryManager:
    def __init__(
        self,
        project_md_path: str | Path = DEFAULT_PROJECT_MD_PATH,
        model: Optional[str] = None,
    ):
        self.project_md_path = Path(project_md_path)
        self.model = model
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        self.project_md_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.project_md_path.exists():
            self.project_md_path.write_text(PROJECT_MD_TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Đọc PROJECT.md để đưa vào system prompt mỗi turn."""
        return self.project_md_path.read_text(encoding="utf-8")

    def update(self, recent_conversation: str) -> dict:
        """Nhờ model chọn project facts bền vững rồi append nếu có."""
        prompt = DIFF_PROMPT_TEMPLATE.format(
            project_md=self.read(),
            recent_conversation=recent_conversation,
        )
        raw_response = llm.complete_text(prompt, model=self.model)
        try:
            diff = json.loads(raw_response)
        except json.JSONDecodeError:
            return {"project_md_append": "", "error": "invalid_json"}

        project_append = (diff.get("project_md_append") or "").strip()
        if project_append:
            with self.project_md_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{project_append}\n")
        return {"project_md_append": project_append}
