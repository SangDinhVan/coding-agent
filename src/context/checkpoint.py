"""Validated, atomically persisted compaction checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_LIST_FIELDS = (
    "progress",
    "decisions",
    "constraints",
    "blockers",
    "remaining_work",
    "critical_references",
    "verification",
)
_FIELDS = {
    "schema_version",
    "session_id",
    "covers_through_seq",
    "created_at",
    "goal",
    *_LIST_FIELDS,
}


class CheckpointCorruptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompactionCheckpoint:
    schema_version: int
    session_id: str
    covers_through_seq: int
    created_at: str
    goal: str
    progress: tuple[str, ...]
    decisions: tuple[str, ...]
    constraints: tuple[str, ...]
    blockers: tuple[str, ...]
    remaining_work: tuple[str, ...]
    critical_references: tuple[str, ...]
    verification: tuple[str, ...]
    provenance_generation_ids: tuple[str, ...] = ()
    security_state_version: int = 0

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        session_id: str,
        max_seq: int,
    ) -> "CompactionCheckpoint":
        if not isinstance(data, dict) or set(data) - {'provenance_generation_ids', 'security_state_version'} != _FIELDS:
            raise CheckpointCorruptionError("checkpoint fields do not match schema")
        if data["schema_version"] != SCHEMA_VERSION:
            raise CheckpointCorruptionError("unsupported checkpoint schema version")
        if data["session_id"] != session_id:
            raise CheckpointCorruptionError("checkpoint session does not match journal")
        sequence = data["covers_through_seq"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence <= max_seq:
            raise CheckpointCorruptionError("checkpoint sequence is outside journal bounds")
        for field in ("session_id", "created_at", "goal"):
            if not isinstance(data[field], str):
                raise CheckpointCorruptionError(f"checkpoint field {field} must be a string")
        converted = {}
        provenance = data.get('provenance_generation_ids', [])
        version = data.get('security_state_version', 0)
        if not isinstance(provenance, list) or any(not isinstance(x, str) for x in provenance) or type(version) is not int or version < 0:
            raise CheckpointCorruptionError('invalid security provenance')
        for field in _LIST_FIELDS:
            value = data[field]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise CheckpointCorruptionError(f"checkpoint field {field} must be a string list")
            converted[field] = tuple(value)
        return cls(
            schema_version=SCHEMA_VERSION,
            session_id=data["session_id"],
            covers_through_seq=sequence,
            created_at=data["created_at"],
            goal=data["goal"],
            provenance_generation_ids=tuple(provenance), security_state_version=version,
            **converted,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for field in _LIST_FIELDS:
            data[field] = list(data[field])
        data['provenance_generation_ids'] = list(self.provenance_generation_ids)
        return data

    def to_message(self) -> dict:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {"role": "user", "content": f"[Compaction checkpoint]\n{encoded}"}


class CheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self, session_id: str, max_seq: int) -> CompactionCheckpoint | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CheckpointCorruptionError(f"invalid checkpoint file: {error}") from error
        return CompactionCheckpoint.from_dict(data, session_id=session_id, max_seq=max_seq)

    def save(self, checkpoint: CompactionCheckpoint) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    checkpoint.to_dict(), temporary,
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
