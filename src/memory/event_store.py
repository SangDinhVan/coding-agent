"""
memory/event_store.py — lưu raw history append-only, mỗi chat trong một file JSONL.

Format map thẳng theo OpenAI message style, chỉ thêm `seq` + `ts`. Danh sách
chat được suy ra trực tiếp từ các file, không cần database hay metadata index.
"""

import json
import time
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4


DEFAULT_CHATS_DIR = Path("memory/chats")


def create_chat_path(chats_dir: str | Path = DEFAULT_CHATS_DIR) -> Path:
    """Cấp một path UUID mới; file chỉ được tạo khi có event đầu tiên."""
    directory = Path(chats_dir)
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        path = directory / f"{uuid4()}.jsonl"
        if not path.exists():
            return path


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _chat_title(path: Path, limit: int) -> str:
    for event in EventStore(path)._iter_events():
        if event.get("role") != "user":
            continue
        title = " ".join(_content_text(event.get("content", "")).split())
        if not title:
            continue
        return title if len(title) <= limit else title[: limit - 3].rstrip() + "..."
    return "(không có tiêu đề)"


def list_chats(
    chats_dir: str | Path = DEFAULT_CHATS_DIR,
    title_limit: int = 60,
) -> list[dict]:
    """Liệt kê chat có dữ liệu, mới cập nhật nhất đứng trước."""
    directory = Path(chats_dir)
    if not directory.exists():
        return []
    chats = []
    for path in directory.glob("*.jsonl"):
        if path.stat().st_size == 0:
            continue
        chats.append({
            "id": path.stem,
            "path": path,
            "title": _chat_title(path, title_limit),
            "updated_at": path.stat().st_mtime,
        })
    return sorted(chats, key=lambda chat: chat["updated_at"], reverse=True)


def resolve_chat(
    chats_dir: str | Path = DEFAULT_CHATS_DIR,
    session_id: str | None = None,
) -> Path:
    """Tìm chat gần nhất hoặc chat khớp ID/prefix duy nhất."""
    chats = list_chats(chats_dir)
    if not chats:
        raise ValueError("Chưa có cuộc chat nào để resume.")
    if session_id is None:
        return chats[0]["path"]

    matches = [chat for chat in chats if chat["id"].startswith(session_id)]
    if not matches:
        raise ValueError(f"Không tìm thấy chat có ID '{session_id}'.")
    if len(matches) > 1:
        raise ValueError(f"ID '{session_id}' không duy nhất; hãy nhập thêm ký tự.")
    return matches[0]["path"]


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._load_last_seq()

    def _load_last_seq(self) -> int:
        """Đọc file cũ (nếu có) để resume đúng sequence."""
        if not self.path.exists():
            return 0
        last_seq = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_seq = max(last_seq, json.loads(line).get("seq", 0))
        return last_seq

    def append(
        self,
        role: str,
        content: Optional[object] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
    ) -> dict:
        """Append một event và trả về event vừa ghi."""
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": role,
        }
        if content is not None:
            event["content"] = content
        if tool_calls is not None:
            event["tool_calls"] = tool_calls
        if tool_call_id is not None:
            event["tool_call_id"] = tool_call_id

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read_all(self) -> list[dict]:
        return list(self._iter_events())

    def _iter_events(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def to_messages(self) -> list[dict]:
        messages = []
        for event in self._iter_events():
            msg = {"role": event["role"]}
            for key in ("content", "tool_calls", "tool_call_id"):
                if key in event:
                    msg[key] = event[key]
            messages.append(msg)
        return messages
