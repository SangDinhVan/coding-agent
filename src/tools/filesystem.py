import hashlib
from pathlib import Path
from tools.base import BaseTool, ToolResult
from runtime.models import ReplayPolicy

COMPACT_PREVIEW_LINES = 50


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _before_hash(path: Path) -> str:
    return _hash_bytes(path.read_bytes()) if path.exists() else "__missing__"


class ReadTool(BaseTool):
    replay_policy = ReplayPolicy.REPLAY_SAFE
    def __init__(self):
        self.name = "read"
        self.description = (
            "Đọc nội dung 1 file từ filesystem. Trả về toàn bộ nội dung "
            "kèm số thứ tự dòng."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Đường dẫn tới file cần đọc (relative hoặc absolute).",
                },
            },
            "required": ["path"],
        }

    def execute(self, path: str, **kwargs) -> ToolResult:
        try:
            file_path = Path(path)
            content = file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ToolResult(
                raw=f"FileNotFoundError: {path}",
                compact=f"Cannot find file: {path}",
                success=False,
            )
        except UnicodeDecodeError:
            return ToolResult(
                raw=f"UnicodeDecodeError: {path} is not a valid UTF-8 text file",
                compact=f"Cannot read {path}: not a text file (binary?)",
                success=False,
            )
        except OSError as e:
            return ToolResult(
                raw=f"OSError reading {path}: {e}",
                compact=f"Cannot read {path}: {e}",
                success=False,
            )

        lines = content.splitlines()
        numbered = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))

        # raw: full content có số dòng, dùng cho log/events.jsonl
        raw = numbered

        # compact: nếu file ngắn thì đưa nguyên, nếu dài thì chỉ preview
        # N dòng đầu + báo tổng số dòng, để model tự quyết định có cần
        # đọc thêm range cụ thể không (tương lai có thể thêm param
        # start_line/end_line cho ReadTool).
        if len(lines) <= COMPACT_PREVIEW_LINES:
            compact = numbered
        else:
            preview = "\n".join(
                f"{i + 1}\t{line}" for i, line in enumerate(lines[:COMPACT_PREVIEW_LINES])
            )
            compact = (
                f"{preview}\n"
                f"... ({len(lines) - COMPACT_PREVIEW_LINES} more lines, "
                f"{len(lines)} lines total in {path})"
            )

        return ToolResult(raw=raw, compact=compact, success=True)


class WriteTool(BaseTool):
    replay_policy = ReplayPolicy.RECONCILABLE
    def __init__(self):
        self.name = "write"
        self.description = (
            "Ghi đè (hoặc tạo mới) 1 file với nội dung cho trước. "
            "Tự tạo thư mục cha nếu chưa tồn tại."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Đường dẫn file cần ghi.",
                },
                "content": {
                    "type": "string",
                    "description": "Nội dung sẽ ghi vào file (ghi đè toàn bộ).",
                },
            },
            "required": ["path", "content"],
        }

    def recovery_metadata(self, path: str, content: str, **kwargs) -> dict:
        file_path = Path(path)
        return {
            "path": str(file_path),
            "before_hash": _before_hash(file_path),
            "expected_after_hash": _hash_bytes(content.encode("utf-8")),
        }

    def execute(self, path: str, content: str, **kwargs) -> ToolResult:
        try:
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(
                raw=f"OSError writing {path}: {e}",
                compact=f"Cannot write {path}: {e}",
                success=False,
            )

        num_lines = len(content.splitlines())
        return ToolResult(
            raw=f"Wrote {path} ({num_lines} lines, {len(content)} chars)",
            compact=f"Successfully wrote {path} ({num_lines} lines)",
            success=True,
        )


class EditTool(BaseTool):
    replay_policy = ReplayPolicy.RECONCILABLE
    def __init__(self):
        self.name = "edit"
        self.description = (
            "Sửa 1 phần trong file bằng cách thay old_string bằng new_string. "
            "old_string BẮT BUỘC phải khớp duy nhất (unique) trong file — "
            "nếu khớp nhiều chỗ hoặc không khớp chỗ nào, tool sẽ báo lỗi và "
            "yêu cầu cung cấp thêm context để old_string trở nên unique."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Đường dẫn file cần sửa.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Đoạn text cần tìm, phải khớp CHÍNH XÁC và duy nhất trong file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Đoạn text sẽ thay thế old_string.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    def recovery_metadata(self, path: str, old_string: str, new_string: str, **kwargs) -> dict:
        file_path = Path(path)
        content = file_path.read_text(encoding="utf-8")
        if content.count(old_string) != 1:
            return {}
        expected = content.replace(old_string, new_string, 1).encode("utf-8")
        return {
            "path": str(file_path),
            "before_hash": _hash_bytes(content.encode("utf-8")),
            "expected_after_hash": _hash_bytes(expected),
        }

    def execute(self, path: str, old_string: str, new_string: str, **kwargs) -> ToolResult:
        try:
            file_path = Path(path)
            content = file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ToolResult(
                raw=f"FileNotFoundError: {path}",
                compact=f"Cannot find file: {path}",
                success=False,
            )
        except OSError as e:
            return ToolResult(
                raw=f"OSError reading {path}: {e}",
                compact=f"Cannot read {path}: {e}",
                success=False,
            )

        occurrences = content.count(old_string)

        if occurrences == 0:
            return ToolResult(
                raw=f"old_string not found in {path}:\n{old_string!r}",
                compact=(
                    f"Edit failed: old_string not found in {path}. "
                    f"Read the file again to get the exact current content."
                ),
                success=False,
            )

        if occurrences > 1:
            return ToolResult(
                raw=(
                    f"old_string matched {occurrences} times in {path}, "
                    f"expected exactly 1 match:\n{old_string!r}"
                ),
                compact=(
                    f"Edit failed: old_string matched {occurrences} times in {path}, "
                    f"but must be unique. Add more surrounding context to old_string "
                    f"so it matches exactly one location."
                ),
                success=False,
            )

        new_content = content.replace(old_string, new_string, 1)

        try:
            file_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return ToolResult(
                raw=f"OSError writing {path}: {e}",
                compact=f"Cannot write {path}: {e}",
                success=False,
            )

        return ToolResult(
            raw=f"Edited {path}:\n--- old ---\n{old_string}\n--- new ---\n{new_string}",
            compact=f"Successfully edited {path}",
            success=True,
        )