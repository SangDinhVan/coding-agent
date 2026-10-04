"""Workspace-scoped, user-curated project memory."""

from __future__ import annotations

import fcntl
import os
import re
import json
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


PROJECT_MD_TEMPLATE = """# PROJECT.md

## Overview
(no data yet)

## Architecture
(no data yet)

## Important Decisions
(no data yet)

## Hard Constraints
(no data yet)

## Coding Conventions
(no data yet)

## Known Problems
(no data yet)

## Failed Approaches
(no data yet - this section MUST retain all attempted solutions that failed
so the agent does not repeat past mistakes)

## Curated Memories

"""

_SECTION = "## Curated Memories"
_ENTRY = re.compile(r"^- \[([0-9a-f]{8})\] (.+)$")


@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    fact: str
    provenance_generation_ids: tuple[str, ...] = ()
    security_state_version: int = 0


class MemoryManager:
    def __init__(self, project_md_path: str | Path):
        self.project_md_path = Path(project_md_path)
        self.disclosure_check = None
        self.get_security_state = None
        self.provenance_path = self.project_md_path.with_suffix('.provenance.json')
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        self.project_md_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.project_md_path.parent.chmod(0o700)
        if not self.project_md_path.exists():
            self._write(PROJECT_MD_TEMPLATE)
        else:
            self.project_md_path.chmod(0o600)

    def read(self) -> str:
        text = self.project_md_path.read_text(encoding="utf-8")
        return self.disclosure_check(text, 'memory') if self.disclosure_check else text

    def entries(self) -> tuple[MemoryEntry, ...]:
        text = self.read()
        start, end = self._section_bounds(text)
        entries = []
        metadata = self._provenance()
        for line in text[start:end].splitlines():
            match = _ENTRY.fullmatch(line)
            if match:
                annotation = metadata.get(match.group(1), {})
                entries.append(MemoryEntry(match.group(1), match.group(2), tuple(annotation.get('provenance_generation_ids', ())), annotation.get('security_state_version', 0)))
        return tuple(entries)

    def _provenance(self):
        if not self.provenance_path.exists():
            return {}
        with self.provenance_path.open('rb') as file:
            raw = file.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise ValueError('memory_provenance_budget')
        data = json.loads(raw)
        from runtime.models import SecurityState
        if not isinstance(data, dict):
            raise ValueError('invalid_memory_provenance')
        for record in data.values():
            if not isinstance(record, dict):
                raise ValueError('invalid_memory_provenance')
            SecurityState().merge(record)
        return data

    def security_annotations(self):
        from runtime.models import SecurityState
        state = SecurityState()
        for record in self._provenance().values():
            state.merge(record)
        return asdict(state)

    def _save_provenance(self, entries):
        if not self.get_security_state:
            return
        data = self._provenance()
        annotation = asdict(self.get_security_state())
        for entry in entries:
            data.setdefault(entry.memory_id, annotation)
        encoded = json.dumps(data, sort_keys=True)
        if len(encoded.encode()) > 1024**2:
            raise ValueError('memory_provenance_budget')
        with tempfile.NamedTemporaryFile('w', dir=self.provenance_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.provenance_path)
            finally:
                temporary.unlink(missing_ok=True)

    def remember(self, fact: str) -> tuple[MemoryEntry, bool]:
        normalized = self._normalize(fact)
        with self._mutation_lock():
            entries = list(self.entries())
            duplicate = next(
                (entry for entry in entries if entry.fact.casefold() == normalized.casefold()),
                None,
            )
            if duplicate is not None:
                return duplicate, False
            used_ids = {entry.memory_id for entry in entries}
            memory_id = uuid4().hex[:8]
            while memory_id in used_ids:
                memory_id = uuid4().hex[:8]
            state = self.get_security_state() if self.get_security_state else None
            entry = MemoryEntry(memory_id, normalized, state.provenance_generation_ids if state else (), state.security_state_version if state else 0)
            entries.append(entry)
            self._write_entries(entries)
            return entry, True

    def forget(self, query: str) -> MemoryEntry:
        normalized = self._normalize(query)
        with self._mutation_lock():
            entries = self.entries()
            matches = [
                entry for entry in entries
                if entry.memory_id == normalized.casefold()
                or entry.fact.casefold() == normalized.casefold()
            ]
            if not matches:
                raise ValueError("memory not found")
            if len(matches) != 1:
                raise ValueError("memory query is ambiguous")
            removed = matches[0]
            self._write_entries([entry for entry in entries if entry != removed])
            return removed

    @contextmanager
    def _mutation_lock(self):
        lock_path = self.project_md_path.with_name(f".{self.project_md_path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            lock_path.chmod(0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _normalize(value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("memory fact/query cannot be empty")
        return normalized

    @staticmethod
    def _section_bounds(text: str) -> tuple[int, int]:
        header = text.find(_SECTION)
        if header < 0:
            return len(text), len(text)
        start = header + len(_SECTION)
        next_header = re.search(r"^##\s", text[start:], flags=re.MULTILINE)
        end = start + next_header.start() if next_header else len(text)
        return start, end

    def _write_entries(self, entries: list[MemoryEntry]) -> None:
        text = self.read()
        start, end = self._section_bounds(text)
        if start == len(text) and _SECTION not in text:
            text = text.rstrip() + f"\n\n{_SECTION}"
            start = len(text)
            end = start
        body = "\n\n"
        if entries:
            body += "\n".join(f"- [{entry.memory_id}] {entry.fact}" for entry in entries) + "\n"
        body += "\n"
        full_text = text[:start] + body + text[end:].lstrip("\n")
        if self.disclosure_check:
            self.disclosure_check(full_text, 'memory')
        self._save_provenance(entries)
        self._write(full_text)

    def _write(self, text: str) -> None:
        if self.disclosure_check:
            self.disclosure_check(text, 'memory')
        self.project_md_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.project_md_path.parent.chmod(0o700)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.project_md_path.parent,
                prefix=f".{self.project_md_path.name}.", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.project_md_path)
            self.project_md_path.chmod(0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
