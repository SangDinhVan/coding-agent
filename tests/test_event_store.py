import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.event_store import EventStore, JournalCorruptionError, JournalLockedError


class EventStoreTests(unittest.TestCase):
    def path(self, directory):
        return Path(directory) / "session.jsonl"

    def test_append_event_writes_versioned_envelope_and_fsyncs(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("memory.event_store.os.fsync", wraps=os.fsync) as fsync:
                with EventStore(self.path(directory), session_id="session") as store:
                    event = store.append_event("TurnStarted", "turn", "turn-1", {"goal": "x"}, turn_id="turn-1")
            self.assertEqual(event["schema_version"], 1)
            self.assertEqual(event["seq"], 1)
            self.assertEqual(event["session_id"], "session")
            self.assertTrue(event["ts"].endswith("Z"))
            fsync.assert_called_once()

    def test_message_wrapper_projects_to_provider_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            with EventStore(self.path(directory)) as store:
                store.append("user", "hello")
                store.append("assistant", None, tool_calls=[{"id": "c1"}])
                store.append("tool", "ok", tool_call_id="c1")
                messages = store.to_messages()
            self.assertEqual(messages, [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "content": "ok", "tool_call_id": "c1"},
            ])

    def test_runtime_events_are_not_provider_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            with EventStore(self.path(directory)) as store:
                store.append_event("TurnStarted", "turn", "t1", {"goal": "x"}, turn_id="t1")
                self.assertEqual(store.to_messages(), [])

    def test_legacy_role_records_still_project(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.path(directory)
            path.write_text('{"seq":1,"ts":"old","role":"user","content":"legacy"}\n', encoding="utf-8")
            with EventStore(path) as store:
                self.assertEqual(store.to_messages(), [{"role": "user", "content": "legacy"}])
                self.assertEqual(store.append("assistant", "new")["seq"], 2)

    def test_second_writer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            first = EventStore(self.path(directory))
            try:
                with self.assertRaises(JournalLockedError):
                    EventStore(self.path(directory))
            finally:
                first.close()

    def test_secret_like_argument_keys_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            with EventStore(self.path(directory)) as store:
                event = store.append_event("ToolRequested", "tool_execution", "e1", {
                    "arguments": {"token": "abc", "nested": {"password": "xyz"}, "path": "ok"}
                })
            self.assertEqual(event["payload"]["arguments"], {
                "token": "[REDACTED]", "nested": {"password": "[REDACTED]"}, "path": "ok"
            })

    def test_duplicate_seq_and_unsupported_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.path(directory)
            base = {
                "schema_version": 1, "event_id": "a", "seq": 1, "ts": "2026-01-01T00:00:00Z",
                "session_id": "s", "runtime_instance_id": "r", "event_type": "TurnStarted",
                "aggregate_type": "turn", "aggregate_id": "t", "turn_id": "t",
                "causation_id": None, "correlation_id": "t", "payload": {"goal": "x"},
            }
            path.write_text(json.dumps(base) + "\n" + json.dumps({**base, "event_id": "b"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(JournalCorruptionError, "sequence"):
                EventStore(path)
            path.write_text(json.dumps({**base, "schema_version": 2}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(JournalCorruptionError, "schema"):
                EventStore(path)

    def test_partial_last_line_requires_explicit_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.path(directory)
            path.write_text('{"seq":1,"role":"user","content":"ok"}\n{"seq":', encoding="utf-8")
            with self.assertRaises(JournalCorruptionError) as caught:
                EventStore(path)
            self.assertTrue(caught.exception.repairable)
            EventStore.repair_trailing_partial(path)
            self.assertTrue(path.with_suffix(".jsonl.bak").exists())
            with EventStore(path) as store:
                self.assertEqual(store.to_messages()[0]["content"], "ok")

    def test_malformed_middle_line_is_never_repaired_away(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.path(directory)
            path.write_text('{"seq":1,"role":"user"}\nnot-json\n{"seq":2,"role":"assistant"}\n', encoding="utf-8")
            with self.assertRaises(JournalCorruptionError) as caught:
                EventStore(path)
            self.assertFalse(caught.exception.repairable)
            with self.assertRaises(JournalCorruptionError):
                EventStore.repair_trailing_partial(path)


if __name__ == "__main__":
    unittest.main()

class StrictJournalTests(unittest.TestCase):
    def test_duplicate_event_id_with_increasing_seq_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            base = {
                "schema_version": 1, "event_id": "same", "seq": 1, "ts": "2026-01-01T00:00:00Z",
                "session_id": "s", "runtime_instance_id": "r", "event_type": "TurnStarted",
                "aggregate_type": "turn", "aggregate_id": "t", "turn_id": "t",
                "causation_id": None, "correlation_id": "t", "payload": {"goal": "x"},
            }
            path.write_text(json.dumps(base) + "\n" + json.dumps({**base, "seq": 2, "event_type": "TurnCompleted"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(JournalCorruptionError, "duplicate event ID"):
                EventStore(path)

    def test_secret_is_absent_from_raw_journal_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            with EventStore(path) as store:
                store.append_event("ToolRequested", "tool_execution", "x", {"arguments": {"token": "never-write-this"}})
            self.assertNotIn("never-write-this", path.read_text(encoding="utf-8"))

class ToolCallRedactionTests(unittest.TestCase):
    def test_secret_inside_tool_call_argument_json_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            with EventStore(path) as store:
                store.append("assistant", None, tool_calls=[{
                    "id": "c", "type": "function", "function": {"name": "fake", "arguments": '{"token":"never-write-this","path":"ok"}'},
                }])
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("never-write-this", contents)
            self.assertIn("[REDACTED]", contents)
