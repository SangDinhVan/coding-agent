"""Durable append-only session journal with legacy message compatibility."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

DEFAULT_CHATS_DIR = Path("memory/chats")
SCHEMA_VERSION = 1
_SECRET_KEYS = {"password", "secret", "token", "api_key", "authorization", "credential"}
_MESSAGE_EVENT_TYPES = {
    "UserMessageRecorded",
    "AssistantToolCallsRecorded",
    "ToolMessageRecorded",
    "AssistantMessageRecorded",
}


class JournalError(RuntimeError):
    pass


class JournalLockedError(JournalError):
    pass


class JournalCorruptionError(JournalError):
    def __init__(self, message: str, *, repairable: bool = False):
        super().__init__(message)
        self.repairable = repairable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def create_chat_path(chats_dir: str | Path = DEFAULT_CHATS_DIR) -> Path:
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
    with EventStore(path, writer=False) as store:
        for message in store.to_messages():
            if message.get("role") != "user":
                continue
            title = " ".join(_content_text(message.get("content", "")).split())
            if title:
                return title if len(title) <= limit else title[: limit - 3].rstrip() + "..."
    return "(không có tiêu đề)"


def list_chats(chats_dir: str | Path = DEFAULT_CHATS_DIR, title_limit: int = 60) -> list[dict]:
    directory = Path(chats_dir)
    if not directory.exists():
        return []
    chats = []
    for path in directory.glob("*.jsonl"):
        if path.stat().st_size:
            chats.append({
                "id": path.stem,
                "path": path,
                "title": _chat_title(path, title_limit),
                "updated_at": path.stat().st_mtime,
            })
    return sorted(chats, key=lambda chat: chat["updated_at"], reverse=True)


def resolve_chat(chats_dir: str | Path = DEFAULT_CHATS_DIR, session_id: str | None = None) -> Path:
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
    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str | None = None,
        runtime_instance_id: str | None = None,
        writer: bool = True,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or self.path.stem
        self.runtime_instance_id = runtime_instance_id or str(uuid4())
        self._writer = writer
        self._file = None
        if writer:
            self._file = self.path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                self._file.close()
                self._file = None
                raise JournalLockedError(f"Session journal is already open: {self.path}") from error
        try:
            events = list(self._iter_events())
            self._validate_events(events)
            self._seq = max((event.get("seq", 0) for event in events), default=0)
            typed_sessions = {event["session_id"] for event in events if "event_type" in event}
            if typed_sessions:
                if len(typed_sessions) != 1:
                    raise JournalCorruptionError("multiple session IDs in one journal")
                self.session_id = typed_sessions.pop()
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    @staticmethod
    def repair_trailing_partial(path: str | Path) -> Path:
        path = Path(path)
        data = path.read_bytes()
        try:
            with EventStore(path, writer=False):
                raise JournalCorruptionError("journal does not need repair")
        except JournalCorruptionError as error:
            if not error.repairable:
                raise
        newline = data.rfind(b"\n")
        if newline < 0:
            raise JournalCorruptionError("no complete JSONL record to preserve")
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        path.write_bytes(data[: newline + 1])
        return backup

    def append_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        if self._file is None:
            raise JournalError("journal is not open for writing")
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid4()),
            "seq": self._seq + 1,
            "ts": _utc_now(),
            "session_id": self.session_id,
            "runtime_instance_id": self.runtime_instance_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "turn_id": turn_id,
            "causation_id": causation_id,
            "correlation_id": correlation_id,
            "payload": _redact(payload),
        }
        self._file.seek(0, os.SEEK_END)
        self._file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._seq = event["seq"]
        return event

    def append(
        self,
        role: str,
        content: Optional[object] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
        *,
        turn_id: str | None = None,
        final: bool = False,
    ) -> dict:
        event_type = {
            "user": "UserMessageRecorded",
            "tool": "ToolMessageRecorded",
            "assistant": "AssistantToolCallsRecorded" if tool_calls is not None else "AssistantMessageRecorded",
        }.get(role)
        if event_type is None:
            raise ValueError(f"Unsupported message role: {role}")
        payload: dict[str, Any] = {"role": role}
        if content is not None:
            payload["content"] = content
        if tool_calls is not None:
            payload["tool_calls"] = tool_calls
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        if role == "assistant" and tool_calls is None:
            payload["final"] = final
        return self.append_event(
            event_type,
            "message",
            str(uuid4()),
            payload,
            turn_id=turn_id,
            correlation_id=turn_id,
        )

    def read_all(self) -> list[dict]:
        return list(self._iter_events())

    def _iter_events(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as file:
            lines = file.readlines()
        for index, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                repairable = index == len(lines) and not line.endswith("\n")
                raise JournalCorruptionError(
                    f"invalid JSON at line {index}", repairable=repairable
                ) from error

    @staticmethod
    def _validate_events(events: list[dict]) -> None:
        last_seq = 0
        event_ids: set[str] = set()
        required = {
            "schema_version", "event_id", "seq", "ts", "session_id", "runtime_instance_id",
            "event_type", "aggregate_type", "aggregate_id", "turn_id", "causation_id",
            "correlation_id", "payload",
        }
        for line, event in enumerate(events, 1):
            seq = event.get("seq")
            if not isinstance(seq, int) or seq <= last_seq:
                raise JournalCorruptionError(f"invalid sequence at line {line}: {seq!r}")
            last_seq = seq
            if "event_type" not in event:
                if "role" not in event:
                    raise JournalCorruptionError(f"unknown legacy record at line {line}")
                continue
            missing = required - event.keys()
            if missing:
                raise JournalCorruptionError(f"missing envelope fields at line {line}: {sorted(missing)}")
            if event["schema_version"] != SCHEMA_VERSION:
                raise JournalCorruptionError(f"unsupported schema version at line {line}")
            if event["event_id"] in event_ids:
                raise JournalCorruptionError(f"duplicate event ID at line {line}")
            event_ids.add(event["event_id"])

    def to_messages(self) -> list[dict]:
        messages = []
        for event in self._iter_events():
            if "event_type" not in event:
                payload = event
            elif event["event_type"] in _MESSAGE_EVENT_TYPES:
                payload = event["payload"]
            else:
                continue
            message = {"role": payload["role"]}
            for key in ("content", "tool_calls", "tool_call_id"):
                if key in payload:
                    message[key] = payload[key]
            messages.append(message)
        return messages
