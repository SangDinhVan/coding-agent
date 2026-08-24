"""
memory/event_store.py — EventStore: ghi/đọc raw history (events.jsonl), append-only.

Đây là "Raw Event History" trong kiến trúc 5 lớp state — canonical truth,
KHÔNG BAO GIỜ bị sửa hay xóa bởi bất kỳ lớp nào khác (kể cả Compactor).
Compactor (context/) chỉ ĐỌC dữ liệu này để tạo bản tóm tắt gửi model,
không bao giờ ghi ngược lại vào file này.

Format tối giản: map thẳng theo OpenAI message style, chỉ thêm `seq` + `ts`.
KHÔNG làm event schema phức tạp (importance level, event type riêng...) —
để sau khi thực sự cần.
"""

import json
import time
from pathlib import Path
from typing import Iterator, Optional


class EventStore:
    def __init__(self, path: str = "memory/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._load_last_seq()

    def _load_last_seq(self) -> int:
        """Đọc file cũ (nếu có) để lấy seq cuối cùng, cho phép resume đúng thứ tự."""
        if not self.path.exists():
            return 0
        last_seq = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                last_seq = max(last_seq, event.get("seq", 0))
        return last_seq

    def append(
        self,
        role: str,
        content: Optional[object] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
    ) -> dict:
        """
        Ghi 1 event mới vào events.jsonl, append-only (mở file ở mode "a",
        KHÔNG BAO GIỜ mode "w"). Trả về event vừa ghi.
        """
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
        """Đọc toàn bộ history (kèm seq/ts) — dùng khi cần debug/replay."""
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
        """
        Convert toàn bộ event thành list messages đúng format litellm/OpenAI
        (bỏ seq/ts — 2 field đó chỉ để lưu trữ, không cần gửi cho model).
        Dùng khi resume session cũ hoặc build context mỗi turn.
        """
        messages = []
        for event in self._iter_events():
            msg = {"role": event["role"]}
            if "content" in event:
                msg["content"] = event["content"]
            if "tool_calls" in event:
                msg["tool_calls"] = event["tool_calls"]
            if "tool_call_id" in event:
                msg["tool_call_id"] = event["tool_call_id"]
            messages.append(msg)
        return messages