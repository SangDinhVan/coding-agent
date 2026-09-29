# Memory and Context MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project-isolated curated memory and durable token-aware compaction checkpoints without changing the event journal's role as runtime source of truth.

**Architecture:** Keep the existing JSONL journal and runtime reducer canonical. Resolve project memory from the state root plus workspace identity, persist one validated checkpoint per session, and let a token-aware compactor rebuild provider context from the checkpoint plus uncovered message events. All project-memory mutations remain explicit CLI actions.

**Tech Stack:** Python 3.10+, standard library dataclasses/JSON/filesystem APIs, existing OpenAI client and `tiktoken` helpers, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-29-memory-context-design.md`

## Global Constraints

- The append-only session journal remains canonical for turn, plan, tool, approval, recovery, and completion state.
- State layout is `<state-root>/chats`, `<state-root>/checkpoints`, `<state-root>/projects/<workspace-identity>`, and `<state-root>/sandboxes`.
- State directories use mode `0700`; checkpoint and project-memory files use mode `0600`.
- Checkpoint and project-memory writes use a temporary sibling file, `fsync`, and `os.replace`.
- Default reserves are 16,000 output tokens, 16,000 tool tokens, 8,000 safety tokens, 8,000 checkpoint tokens, and 20,000 older raw-user tokens.
- The newest user message is never truncated; an oversized current message fails explicitly.
- Assistant tool calls and their matching tool results are retained or compacted as one atomic group.
- The old `projects/default/PROJECT.md` is neither read nor migrated automatically.
- Project memory is advisory; current instructions, repository code, configuration, tests, journal state, and runtime projection outrank it.
- No vector search, embedding store, automatic extraction, consolidation, generated skills, or cross-project memory is added.

## Review Focus

- Two workspace identities under one state root must never read or modify each other's `PROJECT.md`; Task 1 and Task 2 add isolation tests.
- Duplicate, missing, or ambiguous `/remember` and `/forget` input must not rewrite the file; Task 2 adds mutation-safety tests.
- Corrupt, stale, or session-mismatched checkpoints must not damage the journal or become runtime truth; Task 3 and Task 5 add recovery tests.
- Multi-result tool-call groups at the compaction boundary must never be split; Task 4 adds boundary tests.
- A current user message or rebuilt context that cannot fit must raise a clear error without truncation or checkpoint replacement; Task 4 and Task 5 add failure-path tests.

---

## File Structure

- `src/core/paths.py`: workspace identity and state-path helpers.
- `src/memory/manager.py`: project-memory document parsing and explicit curated mutations.
- `src/context/checkpoint.py`: checkpoint schema, validation, rendering, and atomic persistence.
- `src/context/compactor.py`: token budgeting, atomic grouping, incremental checkpoint generation, and context rebuilding.
- `src/memory/event_store.py`: sequence-preserving projection of message events.
- `src/agent/loop.py`: memory/checkpoint wiring and `ContextCompacted` persistence.
- `src/runtime/reducer.py`: recognize `ContextCompacted` as a non-state-changing event.
- `src/main.py`: state-root wiring and `/memory`, `/remember`, `/forget` control commands.
- `tests/test_memory_manager.py`: project-memory behavior and isolation.
- `tests/test_checkpoint.py`: checkpoint validation and atomic storage.
- `tests/test_compactor.py`: token-budget and compaction behavior.
- `tests/test_agent_loop.py`: active-context and compaction integration.
- `tests/test_event_store.py`: sequence-preserving message projection.
- `tests/test_main.py`: CLI command routing and production wiring.
- `docs/run_guide.md`: user-facing memory commands and storage paths.
- `docs/source_code_flow_guide_vi.md`: updated context and resume flow.

### Task 1: Resolve Workspace-Scoped State Paths

**Files:**
- Modify: `src/core/paths.py`
- Modify: `tests/test_sandbox_models.py`

**Interfaces:**
- Produces: `workspace_identity(source_workspace: str | Path) -> str`
- Produces: `project_memory_path(state_root: str | Path, identity: str) -> Path`
- Produces: `checkpoint_path(state_root: str | Path, session_id: str) -> Path`
- Consumes: existing session-ID validation and `ControlPaths.default_root()`

- [ ] **Step 1: Write failing path and isolation tests**

Add tests that assert:

```python
identity = workspace_identity(workspace)
self.assertEqual(identity, ControlPaths.create(root, "session-1", workspace).workspace_identity)
self.assertEqual(
    project_memory_path(root, identity),
    Path(root) / "projects" / identity / "PROJECT.md",
)
self.assertEqual(
    checkpoint_path(root, "session-1"),
    Path(root) / "checkpoints" / "session-1.json",
)
```

Also create two temporary workspaces and assert their identities and project-memory paths differ.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m unittest tests.test_sandbox_models.ControlPathsTests -v`

Expected: FAIL because the three helper functions do not exist.

- [ ] **Step 3: Implement the shared path helpers**

In `src/core/paths.py`:

```python
def workspace_identity(source_workspace: str | Path) -> str: ...
def project_memory_path(state_root: str | Path, identity: str) -> Path: ...
def checkpoint_path(state_root: str | Path, session_id: str) -> Path: ...
```

Reuse `_SESSION_ID` for checkpoint validation. Move the existing path/device/inode hashing from `ControlPaths.create()` into `workspace_identity()` and call the helper from `ControlPaths.create()`.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_sandbox_models.ControlPathsTests -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/paths.py tests/test_sandbox_models.py
git commit -m "refactor: centralize workspace state paths"
```

### Task 2: Replace Automatic Memory Writes with Curated CLI Memory

**Files:**
- Modify: `src/memory/manager.py`
- Modify: `src/agent/loop.py`
- Modify: `src/main.py`
- Create: `tests/test_memory_manager.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_main.py`
- Modify: `tests/test_plan_lifecycle.py`
- Modify: `tests/test_recovery.py`
- Modify: `tests/test_sandbox_models.py`

**Interfaces:**
- Consumes: `project_memory_path(state_root, workspace_identity)` from Task 1
- Produces: `MemoryEntry(memory_id: str, fact: str)`
- Produces: `MemoryManager.read() -> str`
- Produces: `MemoryManager.remember(fact: str) -> tuple[MemoryEntry, bool]`, where the boolean reports whether a new entry was written
- Produces: `MemoryManager.forget(query: str) -> MemoryEntry`
- Produces: `Agent(..., state_root: str | Path | None = None, workspace_identity: str | None = None)`
- Produces: `Agent.memory_manager` for CLI control commands

- [ ] **Step 1: Write failing `MemoryManager` tests**

Cover these assertions in `tests/test_memory_manager.py`:

```python
entry, created = manager.remember("  Repository   uses pytest. ")
self.assertTrue(created)
self.assertEqual(entry.fact, "Repository uses pytest.")
self.assertRegex(entry.memory_id, r"^[0-9a-f]{8}$")

same, created = manager.remember("repository uses pytest.")
self.assertFalse(created)
self.assertEqual(same.memory_id, entry.memory_id)

removed = manager.forget(entry.memory_id)
self.assertEqual(removed, entry)
```

Add tests that two managers with different workspace paths under one state root remain isolated, unmanaged sections survive writes, files/directories use `0600`/`0700`, and missing/ambiguous forget queries leave bytes unchanged.

- [ ] **Step 2: Write failing CLI and turn-completion tests**

In `tests/test_main.py`, assert `/memory`, `/remember fact`, and `/forget id` call the memory manager and never call `agent.run_turn()`.

Also assert `_default_agent_factory()` passes the CLI state root and `paths.workspace_identity` into `Agent`.

Construct two agents with the same state root and different workspace identities and assert their system messages contain only their own project facts. Remove obsolete `_update_memory_bg` patches from `tests/test_agent_loop.py`, `tests/test_plan_lifecycle.py`, and `tests/test_recovery.py`; replace them with a shared fake or a `complete_text` non-call assertion where needed.

Update `tests/test_sandbox_models.py` so it no longer imports or expects `DEFAULT_PROJECT_MD_PATH`.

In `tests/test_agent_loop.py`, replace patches of `_update_memory_bg` with an assertion that completing a turn does not call `model.llm.complete_text` for memory maintenance.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `python -m unittest tests.test_memory_manager tests.test_main tests.test_agent_loop tests.test_plan_lifecycle tests.test_recovery tests.test_sandbox_models -v`

Expected: FAIL because curated methods and CLI commands are absent and automatic updates still exist.

- [ ] **Step 4: Implement deterministic project-memory parsing and atomic writes**

In `src/memory/manager.py`:

- remove the model dependency, `DIFF_PROMPT_TEMPLATE`, and `update()`;
- accept an explicit `project_md_path` with no shared-default fallback;
- keep the existing template and add `## Curated Memories`;
- parse only lines matching `- [<8 hex>] <fact>` inside that section;
- normalize facts with `" ".join(fact.split())`;
- preserve all text outside the managed section;
- write through a sibling temporary file, flush, `os.fsync`, `os.replace`, and enforce `0600`.

- [ ] **Step 5: Remove automatic memory extraction from the agent**

In `src/agent/loop.py`, remove `threading`, `_update_memory_bg()`, `_render_recent_for_memory()`, and the post-`TurnCompleted` background update block.

Add `state_root` and `workspace_identity` constructor parameters. Default `state_root` to the event file's parent for isolated tests and default `workspace_identity` by calling Task 1's helper, imported as `compute_workspace_identity`, on `workdir`. Construct `MemoryManager(project_memory_path(...))` and expose it as `self.memory_manager`.

- [ ] **Step 6: Wire the production factory and add CLI control commands**

In `_default_agent_factory()`, pass the real CLI `state_root` and `paths.workspace_identity` into `Agent`.

In `run_repl()` intercept commands before the active-turn and image handling paths:

```text
/memory
/remember <fact>
/forget <id-or-fact>
```

Print deterministic success/error text, do not send commands to the model, and do not terminate the REPL after a memory command.

- [ ] **Step 7: Run focused tests**

Run: `python -m unittest tests.test_memory_manager tests.test_main tests.test_agent_loop tests.test_plan_lifecycle tests.test_recovery tests.test_sandbox_models -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/memory/manager.py src/agent/loop.py src/main.py tests/test_memory_manager.py tests/test_agent_loop.py tests/test_main.py tests/test_plan_lifecycle.py tests/test_recovery.py tests/test_sandbox_models.py
git commit -m "feat: add curated project memory commands"
```

### Task 3: Add Durable Checkpoint Storage and Sequence-Preserving Messages

**Files:**
- Create: `src/context/checkpoint.py`
- Modify: `src/memory/event_store.py`
- Create: `tests/test_checkpoint.py`
- Modify: `tests/test_event_store.py`

**Interfaces:**
- Consumes: `checkpoint_path(state_root, session_id)` from Task 1
- Produces: `CompactionCheckpoint.from_dict(data: dict, *, session_id: str, max_seq: int) -> CompactionCheckpoint`
- Produces: `CompactionCheckpoint.to_dict() -> dict`
- Produces: `CompactionCheckpoint.to_message() -> dict`
- Produces: `CheckpointStore.load(session_id: str, max_seq: int) -> CompactionCheckpoint | None`
- Produces: `CheckpointStore.save(checkpoint: CompactionCheckpoint) -> None`
- Produces: `EventStore.message_records(after_seq: int = 0) -> list[tuple[int, dict]]`
- Produces: `EventStore.last_seq -> int`

- [ ] **Step 1: Write failing checkpoint schema tests**

Test a valid round trip and reject:

```python
with self.assertRaises(CheckpointCorruptionError):
    CompactionCheckpoint.from_dict({**valid, "schema_version": 2}, session_id="s", max_seq=20)
with self.assertRaises(CheckpointCorruptionError):
    CompactionCheckpoint.from_dict({**valid, "session_id": "other"}, session_id="s", max_seq=20)
with self.assertRaises(CheckpointCorruptionError):
    CompactionCheckpoint.from_dict({**valid, "covers_through_seq": 21}, session_id="s", max_seq=20)
```

Also reject missing keys, unknown keys, non-string list elements, and negative sequence values.

- [ ] **Step 2: Write failing persistence tests**

Assert `save()` creates parent mode `0700`, file mode `0600`, calls `os.replace`, and leaves the previous valid file readable when replacement fails. Assert malformed JSON raises `CheckpointCorruptionError` without touching the journal fixture.

- [ ] **Step 3: Write failing message-record projection tests**

Append runtime and message events, then assert:

```python
self.assertEqual(store.message_records(after_seq=message_seq - 1), [
    (message_seq, {"role": "user", "content": "goal"}),
])
```

Legacy message rows must retain their existing `seq` values in this projection.
Assert `store.last_seq` returns the highest validated sequence or `0` for an empty journal.

- [ ] **Step 4: Run focused tests and confirm failure**

Run: `python -m unittest tests.test_checkpoint tests.test_event_store -v`

Expected: FAIL because checkpoint types and `message_records()` do not exist.

- [ ] **Step 5: Implement `CompactionCheckpoint` and `CheckpointStore`**

Use an immutable dataclass with these exact fields:

```python
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
```

`to_message()` returns one system message headed `[Compaction checkpoint]` followed by deterministic JSON. Do not put checkpoint data into the runtime reducer.

- [ ] **Step 6: Implement `EventStore.message_records()`**

Refactor the existing provider-message projection into one private helper used by both `message_records()` and `to_messages()`. Keep runtime-only events excluded and preserve current legacy compatibility.

- [ ] **Step 7: Run focused tests**

Run: `python -m unittest tests.test_checkpoint tests.test_event_store -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/context/checkpoint.py src/memory/event_store.py tests/test_checkpoint.py tests/test_event_store.py
git commit -m "feat: persist validated compaction checkpoints"
```

### Task 4: Build Token-Aware Incremental Compaction

**Files:**
- Modify: `src/context/compactor.py`
- Modify: `tests/test_compactor.py`

**Interfaces:**
- Consumes: `CompactionCheckpoint` from Task 3
- Consumes: `list[tuple[int, dict]]` from `EventStore.message_records()`
- Produces: `ContextBudget.available_input_tokens -> int`
- Produces: `CompactionResult(messages, checkpoint, compacted, input_tokens_before, input_tokens_after, summary_input_tokens, duration_ms)`
- Produces: `Compactor.build_context(*, system_message: dict, message_records: list[tuple[int, dict]], checkpoint: CompactionCheckpoint | None, session_id: str, goal: str) -> CompactionResult`
- Raises: `ContextBudgetExceeded` and `CompactionError`

- [ ] **Step 1: Replace fixed-ratio tests with budget tests**

Add tests for these exact behaviors:

- below-budget input returns the original message sequence and does not call `llm.complete_text`;
- available input equals context window minus the three global reserves;
- the newest user message remains byte-for-byte unchanged;
- older retained user messages total no more than 20,000 tokens;
- a newest user message larger than available input raises `ContextBudgetExceeded`.

- [ ] **Step 2: Add atomic boundary and incremental-summary tests**

Create one assistant message with two tool calls and two following tool results at the retention boundary. Assert all three messages are either raw or summarized together.

Pass a previous checkpoint with `covers_through_seq=10`, records at sequences 11-30, and assert the summary prompt contains the previous checkpoint plus only records 11 through the new cutoff. It must not contain records at sequence 10 or the retained suffix after the cutoff.

- [ ] **Step 3: Add invalid-summary and final-fit tests**

Patch `llm.complete_text` to return malformed JSON, wrong schema, and a checkpoint over 8,000 tokens. Each case must raise `CompactionError` and return no replacement checkpoint.

Also force the rebuilt context over budget and assert it fails rather than truncating a raw message or splitting a tool group.

- [ ] **Step 4: Run focused tests and confirm failure**

Run: `python -m unittest tests.test_compactor -v`

Expected: FAIL because the old compactor exposes only ratio-based `should_compact()` and `compact()`.

- [ ] **Step 5: Implement budget and result dataclasses**

Use these defaults in `ContextBudget`:

```python
reserved_output_tokens = 16_000
reserved_tool_tokens = 16_000
safety_margin_tokens = 8_000
checkpoint_max_tokens = 8_000
recent_user_max_tokens = 20_000
```

Reject a budget whose reserves leave no input capacity.

- [ ] **Step 6: Implement sequence-aware atomic grouping and suffix selection**

Represent a group as its original `(seq, message)` records. An assistant message with tool calls consumes every immediately following tool message whose `tool_call_id` belongs to that assistant call set. Never split the group during cutoff selection.

Walk groups newest-first. Always attempt to retain the group containing the newest user message, then add older groups while both the raw-suffix allowance and older-user allowance remain satisfied.

- [ ] **Step 7: Implement strict incremental checkpoint generation**

Render the previous checkpoint plus compacted records into the existing Vietnamese handoff prompt categories. Require JSON with exactly the narrative keys `goal`, `progress`, `decisions`, `constraints`, `blockers`, `remaining_work`, `critical_references`, and `verification`. Set `schema_version`, `session_id`, `covers_through_seq`, and `created_at` in code, validate the resulting full Task 3 schema, and measure elapsed time with `time.monotonic()`.

- [ ] **Step 8: Rebuild and verify the final provider context**

Return:

```text
system message
+ checkpoint system message, when present
+ raw messages after covers_through_seq
```

Count the rebuilt messages. Raise `ContextBudgetExceeded` if they still exceed available input. Do not persist from the compactor; return the validated checkpoint to the caller.

- [ ] **Step 9: Run focused tests**

Run: `python -m unittest tests.test_compactor -v`

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/context/compactor.py tests/test_compactor.py
git commit -m "feat: add token-aware incremental compaction"
```

### Task 5: Wire Checkpoints and Project Identity into the Agent

**Files:**
- Modify: `src/agent/loop.py`
- Modify: `src/runtime/reducer.py`
- Modify: `tests/test_agent_loop.py`
- Modify: `tests/test_reducer.py`

**Interfaces:**
- Consumes: path helpers from Task 1
- Consumes: curated `MemoryManager` from Task 2
- Consumes: `CheckpointStore` from Task 3
- Consumes: `Compactor.build_context()` from Task 4
- Produces: `ContextCompacted` journal events with measurement payload

- [ ] **Step 1: Write failing project-memory prompt tests**

In `tests/test_agent_loop.py`, assert the system text labels project memory as advisory and states that instructions, repository state, tests, journal state, and runtime state outrank it.

- [ ] **Step 2: Write failing context/resume integration tests**

Cover:

```python
self.assertEqual(context[0]["role"], "system")
self.assertIn("advisory", context[0]["content"].lower())
self.assertEqual(context[1], checkpoint.to_message())
self.assertEqual(context[2:], uncovered_messages)
```

Add tests that below-budget context creates no checkpoint/event, successful compaction saves the returned checkpoint before the main model call, and a resumed agent uses only messages after `covers_through_seq`.

- [ ] **Step 3: Write failing corruption and measurement tests**

Place malformed or session-mismatched checkpoint JSON at the production path. Agent construction and runtime replay must succeed, `_build_context()` must fall back to the journal, and the journal bytes before the next append must remain unchanged.

For successful compaction, assert the journal gets one `ContextCompacted` event containing:

```python
{
    "previous_covers_through_seq": 0,
    "covers_through_seq": expected_cutoff,
    "input_tokens_before": unittest.mock.ANY,
    "input_tokens_after": unittest.mock.ANY,
    "summary_input_tokens": unittest.mock.ANY,
    "duration_ms": unittest.mock.ANY,
}
```

- [ ] **Step 4: Run focused tests and confirm failure**

Run: `python -m unittest tests.test_agent_loop tests.test_reducer -v`

Expected: FAIL because the agent still uses shared default memory and ephemeral compaction.

- [ ] **Step 5: Add checkpoint loading to the existing agent wiring**

In `Agent.__init__()`:

- create `CheckpointStore(checkpoint_path(...))`;
- load a checkpoint only for `event_store.session_id` and `event_store.last_seq`;
- retain the Task 2 state-root, workspace-identity, and `MemoryManager` construction unchanged.

- [ ] **Step 6: Replace `_build_context()` with durable context construction**

Build the system message with an explicit advisory-memory precedence statement, obtain `message_records()`, call `Compactor.build_context()`, and when it returns a new checkpoint:

1. save it atomically;
2. append `ContextCompacted` with the result measurements;
3. return the already rebuilt provider messages.

Do not add checkpoints to runtime state and do not modify or delete journal messages.

- [ ] **Step 7: Handle invalid checkpoints as unavailable**

Catch only `CheckpointCorruptionError` while loading. Preserve runtime replay, emit a concise warning, and continue with `checkpoint=None`. Do not catch journal corruption or silently use `projects/default`.

- [ ] **Step 8: Register the measurement event as a reducer no-op**

Add `ContextCompacted` to the reducer's known no-op events. It must not alter turn, plan, tool, or active-session state.

- [ ] **Step 9: Run focused tests**

Run: `python -m unittest tests.test_agent_loop tests.test_reducer -v`

Expected: PASS.

- [ ] **Step 10: Run all memory and runtime regression tests**

Run: `python -m unittest tests.test_memory_manager tests.test_checkpoint tests.test_compactor tests.test_event_store tests.test_agent_loop tests.test_reducer tests.test_recovery tests.test_plan_lifecycle tests.test_tool_lifecycle -v`

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/agent/loop.py src/runtime/reducer.py tests/test_agent_loop.py tests/test_reducer.py
git commit -m "feat: integrate durable memory context"
```

### Task 6: Document and Verify the Complete MVP

**Files:**
- Modify: `docs/run_guide.md`
- Modify: `docs/source_code_flow_guide_vi.md`
- Modify: `docs/memory.md`

**Interfaces:**
- Consumes: completed behavior from Tasks 1-5
- Produces: user and architecture documentation matching the shipped commands, paths, budgets, and failure behavior

- [ ] **Step 1: Update the run guide**

Document:

- `/memory`, `/remember <fact>`, and `/forget <id-or-fact>`;
- project-memory and checkpoint locations below `--state-root`;
- the absence of automatic memory writes;
- the fact that checkpoint failures are visible and never delete the journal.

- [ ] **Step 2: Update the source-flow guide**

Replace references to `projects/default/PROJECT.md`, the 70% trigger, ten-message retention, and background `MemoryManager.update()`. Add the incremental checkpoint/resume flow and `ContextCompacted` measurement event.

- [ ] **Step 3: Mark the proposal document as implemented**

In `docs/memory.md`, change the status only after all tests pass. Keep the design history and non-goals intact.

- [ ] **Step 4: Run documentation consistency searches**

Run: `rg -n "projects/default|KEEP_RECENT_MESSAGES|COMPACT_THRESHOLD_RATIO|updating PROJECT.md|MemoryManager.update" src tests docs`

Expected: no live-code or current-guide references; historical trace documents may still describe old captured behavior and must be clearly labeled as historical rather than rewritten.

- [ ] **Step 5: Run the complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS with zero failures and zero errors.

- [ ] **Step 6: Run formatting and repository checks**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: only the files intentionally changed by this implementation are listed.

- [ ] **Step 7: Commit**

```bash
git add docs/run_guide.md docs/source_code_flow_guide_vi.md docs/memory.md
git commit -m "docs: explain durable memory workflow"
```
