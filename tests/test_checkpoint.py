import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context.checkpoint import (
    CheckpointCorruptionError,
    CheckpointStore,
    CompactionCheckpoint,
)


def valid_checkpoint(**changes):
    data = {
        "schema_version": 1,
        "session_id": "s",
        "covers_through_seq": 10,
        "created_at": "2026-09-29T10:00:00Z",
        "goal": "Fix auth",
        "progress": ["Read auth.py"],
        "decisions": ["Keep middleware"],
        "constraints": ["Stable response"],
        "blockers": [],
        "remaining_work": ["Add integration test"],
        "critical_references": ["src/auth.py"],
        "verification": ["Unit tests pass"],
    }
    data.update(changes)
    return data


class CompactionCheckpointTests(unittest.TestCase):
    def test_valid_checkpoint_round_trips_and_renders_deterministically(self):
        checkpoint = CompactionCheckpoint.from_dict(valid_checkpoint(), session_id="s", max_seq=20)
        self.assertEqual(checkpoint.to_dict(), valid_checkpoint() | {'provenance_generation_ids': [], 'security_state_version': 0})
        self.assertEqual(checkpoint.to_message(), {
            "role": "user",
            "content": "[Compaction checkpoint]\n" + json.dumps(
                checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        })

    def test_rejects_wrong_version_session_or_sequence(self):
        for data in (
            valid_checkpoint(schema_version=2),
            valid_checkpoint(session_id="other"),
            valid_checkpoint(covers_through_seq=21),
            valid_checkpoint(covers_through_seq=-1),
        ):
            with self.subTest(data=data), self.assertRaises(CheckpointCorruptionError):
                CompactionCheckpoint.from_dict(data, session_id="s", max_seq=20)

    def test_rejects_missing_unknown_and_invalid_narrative_fields(self):
        missing = valid_checkpoint()
        missing.pop("goal")
        invalid_cases = [
            missing,
            valid_checkpoint(extra="value"),
            valid_checkpoint(progress=["ok", 3]),
            valid_checkpoint(goal=3),
        ]
        for data in invalid_cases:
            with self.subTest(data=data), self.assertRaises(CheckpointCorruptionError):
                CompactionCheckpoint.from_dict(data, session_id="s", max_seq=20)


class CheckpointStoreTests(unittest.TestCase):
    def test_save_load_uses_private_modes_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "s.json"
            store = CheckpointStore(path)
            checkpoint = CompactionCheckpoint.from_dict(valid_checkpoint(), session_id="s", max_seq=20)
            with patch("context.checkpoint.os.replace", wraps=__import__("os").replace) as replace:
                store.save(checkpoint)
            self.assertEqual(store.load("s", 20), checkpoint)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            replace.assert_called_once()

    def test_failed_replace_preserves_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.json"
            store = CheckpointStore(path)
            first = CompactionCheckpoint.from_dict(valid_checkpoint(), session_id="s", max_seq=20)
            second = CompactionCheckpoint.from_dict(
                valid_checkpoint(covers_through_seq=12), session_id="s", max_seq=20
            )
            store.save(first)
            with patch("context.checkpoint.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.save(second)
            self.assertEqual(store.load("s", 20), first)

    def test_missing_returns_none_and_malformed_json_fails_without_touching_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.json"
            journal = Path(directory) / "s.jsonl"
            journal.write_text("journal remains\n", encoding="utf-8")
            store = CheckpointStore(path)
            self.assertIsNone(store.load("s", 20))
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(CheckpointCorruptionError):
                store.load("s", 20)
            self.assertEqual(journal.read_text(encoding="utf-8"), "journal remains\n")


if __name__ == "__main__":
    unittest.main()
