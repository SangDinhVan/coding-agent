import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import Agent
from context.checkpoint import CheckpointStore, CompactionCheckpoint
from context.compactor import CompactionResult
from core.paths import checkpoint_path
from memory.event_store import EventStore
from sandbox.models import WorkspaceMode
from tools.base import ToolResult
from tests.fakes import FakeTool, stream_text, stream_tool_call


class FakeRegistry:
    def __init__(self, tool=None):
        self.tool = tool

    def schemas(self):
        return []

    def get(self, name):
        return self.tool if self.tool is not None and name == self.tool.name else None


def make_checkpoint(session_id: str, covers: int, max_seq: int | None = None):
    return CompactionCheckpoint.from_dict({
        "schema_version": 1,
        "session_id": session_id,
        "covers_through_seq": covers,
        "created_at": "2026-09-29T10:00:00Z",
        "goal": "goal",
        "progress": ["old work"],
        "decisions": [],
        "constraints": [],
        "blockers": [],
        "remaining_work": ["continue"],
        "critical_references": [],
        "verification": [],
    }, session_id=session_id, max_seq=max_seq if max_seq is not None else covers)


class AgentLoopCharacterizationTests(unittest.TestCase):
    def agent(self, directory: str, tool=None) -> Agent:
        agent = Agent(
            str(Path(directory) / "events.jsonl"),
            workdir=directory,
            tool_registry=FakeRegistry(tool),
        )
        self.addCleanup(agent.event_store.close)
        return agent

    def test_direct_text_completes_and_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")), patch(
                "agent.loop.llm.complete_text"
            ) as complete_text:
                self.assertEqual(agent.run_turn("goal"), "done")
            complete_text.assert_not_called()
            self.assertEqual(agent.turn_state.status.value, "completed")
            self.assertEqual([m["role"] for m in agent.event_store.to_messages()], ["user", "assistant"])

    def test_project_memory_is_isolated_by_workspace_identity(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            first = Agent(
                str(Path(state) / "first.jsonl"), workdir=workspace,
                state_root=state, workspace_identity="first",
            )
            second = Agent(
                str(Path(state) / "second.jsonl"), workdir=workspace,
                state_root=state, workspace_identity="second",
            )
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.memory_manager.remember("Only first workspace knows this.")
            self.assertIn("Only first workspace knows this.", first._build_system_message()["content"])
            self.assertNotIn("Only first workspace knows this.", second._build_system_message()["content"])

    def test_tool_result_is_sent_back_before_final_response(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = FakeTool()
            agent = self.agent(directory, tool)
            responses = [stream_tool_call("call-1", "fake"), stream_text("finished")]
            with patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(agent.run_turn("goal"), "finished")
            messages = agent.event_store.to_messages()
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(messages[2]["tool_call_id"], "call-1")
            self.assertEqual(messages[2]["content"], "compact-ok")

    def test_failed_tool_result_does_not_crash_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = FakeTool(ToolResult("raw-error", "compact-error", False))
            agent = self.agent(directory, tool)
            responses = [stream_tool_call("call-1", "fake"), stream_text("recovered")]
            with patch("agent.loop.llm.complete", side_effect=responses):
                self.assertEqual(agent.run_turn("goal"), "recovered")
            self.assertEqual(tool.calls, 1)

    def test_max_iterations_stops_after_exact_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = FakeTool()
            agent = self.agent(directory, tool)
            with patch("agent.loop.llm.complete", return_value=stream_tool_call("call-1", "fake")) as complete:
                self.assertEqual(agent.run_turn("goal", max_iterations=2), "")
            self.assertEqual(complete.call_count, 2)
            self.assertEqual(agent.turn_state.status.value, "failed")

    def test_interrupt_does_not_persist_partial_text_as_final(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)

            def interrupted_stream():
                yield from stream_text("partial")
                raise KeyboardInterrupt

            with patch("agent.loop.llm.complete", return_value=interrupted_stream()):
                self.assertEqual(agent.run_turn("goal"), "")
            self.assertEqual([m["role"] for m in agent.event_store.to_messages()], ["user"])

    def test_turn_reports_model_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")), patch("builtins.print") as output:
                agent.run_turn("goal")
            self.assertTrue(any("waiting for response" in str(call) for call in output.call_args_list))

    def test_large_tool_call_reports_streamed_size(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            chunks = stream_tool_call("call-1", "fake", '{"content":"' + "x" * 9000 + '"}')
            with patch("builtins.print") as output:
                _, calls = agent._consume_stream(chunks)
            self.assertEqual(len(calls[0].function.arguments), 9014)
            self.assertTrue(any("receiving tool call" in str(call) and "KiB" in str(call) for call in output.call_args_list))


if __name__ == "__main__":
    unittest.main()

class DurableTurnLifecycleTests(AgentLoopCharacterizationTests):
    def event_types(self, agent):
        return [e["event_type"] for e in agent.event_store.read_all()]

    def test_direct_text_records_started_final_and_completed_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                agent.run_turn("goal")
            self.assertEqual(self.event_types(agent), [
                "UserMessageRecorded", "TurnStarted", "TurnIterationAdvanced",
                "CompletionRequested", "AssistantMessageRecorded", "TurnCompleted",
            ])

    def test_iteration_is_durable_and_max_limit_has_structured_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = FakeTool()
            agent = self.agent(directory, tool)
            with patch("agent.loop.llm.complete", return_value=stream_tool_call("c", "fake")):
                agent.run_turn("goal", max_iterations=2)
            self.assertEqual(agent.turn_state.iteration, 2)
            self.assertEqual(agent.turn_state.error.category, "max_iterations")

    def test_model_exception_records_failed_and_remains_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            with patch("agent.loop.llm.complete", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    agent.run_turn("goal")
            self.assertEqual(agent.turn_state.status.value, "failed")
            self.assertEqual(self.event_types(agent)[-1], "TurnFailed")

    def test_stream_interrupt_records_interrupted_without_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = self.agent(directory)
            def interrupted():
                yield from stream_text("partial")
                raise KeyboardInterrupt
            with patch("agent.loop.llm.complete", return_value=interrupted()):
                agent.run_turn("goal")
            self.assertEqual(agent.turn_state.status.value, "interrupted")
            self.assertNotIn("AssistantMessageRecorded", self.event_types(agent))

class SandboxAgentWiringTests(unittest.TestCase):
    def test_agent_uses_injected_session_bound_registry_and_stops_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = unittest.mock.MagicMock()
            sandbox.status.value = "running"
            sandbox.backend.image = "image@sha256:" + "a" * 64
            tool_registry = unittest.mock.MagicMock()
            tool_registry.schemas.return_value = []
            tool = FakeTool()
            tool_registry.get.return_value = tool
            agent = Agent(str(Path(directory) / "events.jsonl"), workdir=directory, sandbox=sandbox, tool_registry=tool_registry)
            with patch("agent.loop.llm.complete", return_value=stream_text("done")):
                agent.run_turn("goal")
            agent.close()
            sandbox.stop.assert_called_once_with("agent_close")
            tool_registry.schemas.assert_called()

    def test_workspace_projection_uses_sandbox_metadata_without_host_git(self):
        from agent.state import WorkspaceState
        sandbox = unittest.mock.MagicMock()
        sandbox.status.value = "running"
        sandbox.backend.image = "repo@sha256:" + "a" * 64
        sandbox.paths.baseline_manifest.read_text.return_value = '{"included":[1,2],"excluded":[3],"rejected":[]}'
        with patch("subprocess.run", side_effect=AssertionError("host git forbidden")):
            rendered = WorkspaceState(sandbox=sandbox).render()
        self.assertIn("network: none", rendered)
        self.assertIn("included files: 2", rendered)


class AgentFinalizationTests(unittest.TestCase):
    def agent(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sandbox = unittest.mock.MagicMock()
        sandbox.status = "running"
        sandbox.mode = WorkspaceMode.SHADOW
        sandbox.backend.image = "image@sha256:" + "a" * 64
        sandbox.source_workspace = Path(directory.name)
        sandbox.paths = unittest.mock.MagicMock()
        sandbox.prepare_changes.return_value = unittest.mock.MagicMock(
            change_set_hash="abc", approvable=True,
        )
        tool_registry = unittest.mock.MagicMock()
        tool_registry.schemas.return_value = []
        agent = Agent(
            str(Path(directory.name) / "events.jsonl"),
            workdir=directory.name,
            sandbox=sandbox,
            tool_registry=tool_registry,
        )
        self.addCleanup(agent.event_store.close)
        return agent, sandbox

    def test_restores_exact_changes_when_session_is_already_sealed(self):
        from sandbox.models import SandboxStatus

        agent, sandbox = self.agent()
        sandbox.status = SandboxStatus.SEALED
        restored = unittest.mock.MagicMock(change_set_hash="abc", approvable=True)
        agent.event_store.close()
        with patch("agent.loop.load_changeset", return_value=restored) as load:
            replacement = Agent(
                str(agent.event_store.path),
                workdir=str(sandbox.source_workspace),
                sandbox=sandbox,
                tool_registry=agent.tool_registry,
            )
        self.addCleanup(replacement.event_store.close)
        self.assertIs(replacement.last_changes, restored)
        load.assert_called_once_with(sandbox.paths)

    def test_prepare_changes_remembers_exact_sealed_set(self):
        agent, sandbox = self.agent()
        changes = agent.prepare_changes()
        self.assertIs(agent.last_changes, changes)
        sandbox.prepare_changes.assert_called_once_with()

    def test_apply_uses_exact_remembered_set_then_destroys(self):
        from sandbox.models import ApplyResult

        agent, sandbox = self.agent()
        changes = agent.prepare_changes()
        expected = ApplyResult(True, "abc", message="applied")
        with patch("agent.loop.apply_changeset", return_value=expected) as apply:
            result = agent.apply_changes("abc", "reviewed")
        self.assertIs(result, expected)
        apply.assert_called_once_with(
            sandbox.paths, sandbox.source_workspace, changes, "abc", "reviewed",
        )
        sandbox.destroy.assert_called_once_with("applied")

    def test_failed_apply_preserves_sandbox_for_recovery(self):
        agent, sandbox = self.agent()
        agent.prepare_changes()
        with patch("agent.loop.apply_changeset", side_effect=RuntimeError("conflict")):
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                agent.apply_changes("abc", "reviewed")
        sandbox.destroy.assert_not_called()

    def test_discard_destroys_without_preparing_changes(self):
        agent, sandbox = self.agent()
        agent.discard()
        sandbox.destroy.assert_called_once_with("discard")
        sandbox.prepare_changes.assert_not_called()


class DurableContextTests(unittest.TestCase):
    def test_system_message_marks_project_memory_advisory_and_lower_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(str(Path(directory) / "session.jsonl"), workdir=directory)
            self.addCleanup(agent.close)
            content = agent._build_system_message()["content"].lower()
            for phrase in ("advisory", "instructions", "repository", "tests", "journal", "runtime"):
                self.assertIn(phrase, content)

    def test_resume_context_is_checkpoint_plus_only_uncovered_messages(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            events_path = Path(state) / "session.jsonl"
            with EventStore(events_path) as store:
                store.append("user", "old user")
                store.append("assistant", "old assistant")
                store.append("user", "new user")
            saved = make_checkpoint("session", covers=2, max_seq=3)
            CheckpointStore(checkpoint_path(state, "session")).save(saved)
            agent = Agent(
                str(events_path), workdir=workspace,
                state_root=state, workspace_identity="workspace",
            )
            self.addCleanup(agent.close)
            context = agent._build_context()
            self.assertEqual(context[0]["role"], "system")
            self.assertIn("advisory", context[0]["content"].lower())
            self.assertEqual(context[1], saved.to_message())
            self.assertEqual(context[2:], [{"role": "user", "content": "new user"}])

    def test_below_budget_context_creates_no_checkpoint_or_event(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            events_path = Path(state) / "session.jsonl"
            agent = Agent(
                str(events_path), workdir=workspace,
                state_root=state, workspace_identity="workspace",
            )
            self.addCleanup(agent.close)
            agent.event_store.append("user", "small")
            agent._build_context()
            self.assertFalse(checkpoint_path(state, "session").exists())
            self.assertFalse(any(
                event.get("event_type") == "ContextCompacted"
                for event in agent.event_store.read_all()
            ))

    def test_successful_compaction_is_saved_and_measured_before_main_model_call(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            events_path = Path(state) / "session.jsonl"
            agent = Agent(
                str(events_path), workdir=workspace,
                state_root=state, workspace_identity="workspace",
                tool_registry=FakeRegistry(),
            )
            self.addCleanup(agent.close)
            compacted = make_checkpoint("session", covers=1, max_seq=1)
            result = CompactionResult(
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "goal"}],
                checkpoint=compacted,
                compacted=True,
                input_tokens_before=100,
                input_tokens_after=40,
                summary_input_tokens=60,
                duration_ms=12,
            )
            agent.compactor = unittest.mock.MagicMock()
            agent.compactor.build_context.return_value = result

            def complete_after_checkpoint(**kwargs):
                self.assertTrue(checkpoint_path(state, "session").exists())
                self.assertTrue(any(
                    event.get("event_type") == "ContextCompacted"
                    for event in agent.event_store.read_all()
                ))
                return stream_text("done")

            with patch("agent.loop.llm.complete", side_effect=complete_after_checkpoint):
                self.assertEqual(agent.run_turn("goal"), "done")

            compact_event = next(
                event for event in agent.event_store.read_all()
                if event.get("event_type") == "ContextCompacted"
            )
            self.assertEqual(compact_event["payload"], {
                "previous_covers_through_seq": 0,
                "covers_through_seq": 1,
                "input_tokens_before": 100,
                "input_tokens_after": 40,
                "summary_input_tokens": 60,
                "duration_ms": 12,
            })

    def test_invalid_checkpoint_is_ignored_without_rewriting_journal(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            events_path = Path(state) / "session.jsonl"
            with EventStore(events_path) as store:
                store.append("user", "goal")
            before = events_path.read_bytes()
            path = checkpoint_path(state, "session")
            path.parent.mkdir(parents=True)
            path.write_text("{bad", encoding="utf-8")
            with patch("builtins.print") as output:
                agent = Agent(
                    str(events_path), workdir=workspace,
                    state_root=state, workspace_identity="workspace",
                )
            self.addCleanup(agent.close)
            self.assertIsNone(agent.checkpoint)
            self.assertEqual(events_path.read_bytes(), before)
            self.assertEqual(agent._build_context()[-1], {"role": "user", "content": "goal"})
            self.assertTrue(any("checkpoint" in str(call).lower() for call in output.call_args_list))

    def test_session_mismatched_checkpoint_is_ignored(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as workspace:
            events_path = Path(state) / "session.jsonl"
            with EventStore(events_path) as store:
                store.append("user", "goal")
            CheckpointStore(checkpoint_path(state, "session")).save(
                make_checkpoint("other", covers=1, max_seq=1)
            )
            with patch("builtins.print"):
                agent = Agent(
                    str(events_path), workdir=workspace,
                    state_root=state, workspace_identity="workspace",
                )
            self.addCleanup(agent.close)
            self.assertIsNone(agent.checkpoint)
