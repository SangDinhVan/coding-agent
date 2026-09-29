# Memory and Context MVP Design

> Status: approved design, ready for implementation planning.
>
> Source: [`docs/memory.md`](../../memory.md). This document turns that
> proposal into an implementation contract for the current repository without
> adding the optional research features described there.

## Goal

The memory subsystem must let the coding agent:

1. continue a long session when the model context approaches its limit;
2. resume the same session after the process exits;
3. share durable facts between sessions of the same workspace;
4. prevent memory from one workspace from being loaded into another.

The implementation keeps four concepts separate:

- the append-only session journal records what happened;
- a compaction checkpoint records enough structured state to continue one
  session;
- project memory records curated facts reusable by sessions in one workspace;
- canonical instructions remain authoritative and cannot be overwritten by
  generated memory.

## Scope

The MVP includes:

- project-scoped `PROJECT.md` storage keyed by the existing workspace identity;
- explicit `/memory`, `/remember`, and `/forget` CLI commands;
- removal of automatic background memory writes after every completed turn;
- token-budget-based context compaction;
- persisted, structured compaction checkpoints per session;
- incremental compaction using `covers_through_seq`;
- atomic retention of assistant tool calls and their tool results;
- resume from checkpoint plus uncovered journal messages;
- compaction measurements sufficient for the TLCN evaluation.

The MVP does not include:

- vector databases, embeddings, or semantic retrieval;
- automatic memory extraction or consolidation;
- generated skills;
- cross-project or multi-machine memory sharing;
- a new canonical-instruction discovery system;
- migration of the old shared `projects/default/PROJECT.md` into a workspace.

The old shared file is left untouched but is no longer read. Automatic
migration could assign facts to the wrong project, so an explicit user action
is required to preserve any useful content from it.

## Current Gaps

The current code already has a durable event journal and runtime replay, but
the memory path still has four mismatches with the target design:

- `MemoryManager` always uses `projects/default/PROJECT.md`;
- `MemoryManager.update()` asks the model to append facts after every turn;
- `Compactor` triggers at a fixed ratio and retains ten messages rather than a
  token budget;
- compaction output exists only for the current request and is not persisted.

The event journal remains the canonical source of session and runtime truth.
This design does not move lifecycle state into `PROJECT.md` or checkpoints.

## Storage Layout

The existing state root remains the owner of all persistent control data:

```text
<state-root>/
├── chats/
│   └── <session-id>.jsonl
├── checkpoints/
│   └── <session-id>.json
├── projects/
│   └── <workspace-identity>/
│       └── PROJECT.md
└── sandboxes/
    └── <session-id>/
```

Directories are private (`0700`) and files are private (`0600`). Checkpoint and
project-memory writes use a temporary sibling file, `fsync`, and `os.replace`
so a crash cannot leave a partially written canonical file.

The workspace identity remains the SHA-256 of the resolved workspace path,
device, and inode. Moving or cloning a repository can therefore create a new
identity; that limitation is accepted for the local MVP.

## Project Memory

### File contract

Each workspace owns exactly one file:

```text
<state-root>/projects/<workspace-identity>/PROJECT.md
```

The document keeps the existing human-readable sections and adds one managed
section for deterministic CLI operations:

```markdown
## Curated Memories

- [a1b2c3d4] Repository tests run with `pytest`.
- [e5f6a7b8] Public authentication response schemas must not change.
```

Facts outside `Curated Memories` remain readable and are preserved when the
managed section changes. Managed entries use an eight-character hexadecimal ID
generated from UUID data; IDs are only local handles, not stable content
hashes.

### Commands

- `/memory` prints the current project memory without calling the model.
- `/remember <fact>` normalizes surrounding and repeated whitespace, rejects an
  empty fact, and appends one managed entry.
- `/remember` is idempotent for an exact case-insensitive fact match and returns
  the existing entry instead of writing a duplicate.
- `/forget <id-or-fact>` removes one exact ID match or one exact
  case-insensitive fact match.
- `/forget` fails without changing the file when there is no match or the query
  is ambiguous.

There is no model-authored write path in the MVP. Repository code,
configuration, tests, current user instructions, and runtime state all outrank
project memory. The system prompt labels `PROJECT.md` as advisory and states
this precedence explicitly.

## Compaction Checkpoint

Each session may own one latest checkpoint:

```json
{
  "schema_version": 1,
  "session_id": "session-a",
  "covers_through_seq": 128,
  "created_at": "2026-09-29T10:00:00Z",
  "goal": "Sửa authentication flow",
  "progress": ["Đã sửa verify_token()"],
  "decisions": ["Giữ JWT validation trong middleware"],
  "constraints": ["Không thay đổi response schema"],
  "blockers": [],
  "remaining_work": ["Thêm integration test cho expired token"],
  "critical_references": ["src/auth.py:verify_token"],
  "verification": ["Unit tests đã pass"]
}
```

Validation is strict:

- `schema_version` must equal `1`;
- `session_id` must match the journal session;
- `covers_through_seq` must be a non-negative integer no greater than the last
  journal sequence;
- all narrative fields must be strings or lists of strings as shown;
- unknown or missing fields are rejected.

A missing checkpoint means the whole journal is initially uncovered. A corrupt
or session-mismatched checkpoint is ignored for context construction and
replaced only after a new valid checkpoint is generated from the journal. The
journal is never truncated or rewritten.

## Context Budget

The default budget is derived from the configured model context window:

```text
available_input_budget
= context_window
- reserved_output_tokens      (16,000)
- reserved_tool_tokens        (16,000)
- safety_margin_tokens         (8,000)
```

The implementation also reserves at most `8,000` input tokens for the rendered
checkpoint and keeps at most `20,000` tokens of older raw user messages after
compaction. The newest user message is always retained verbatim when it fits in
the total available input budget; the `20,000` limit applies to additional
older user messages.

These values are explicit configuration constants in the context module. They
can become CLI or environment configuration later if evaluation shows a real
need; the MVP does not add configuration surface pre-emptively.

Token estimation continues to use the existing `model.llm` helpers. Message
content and tool-call payloads are counted. The tool schema itself is covered
by the reserved tool budget rather than recalculated on every turn.

## Context Construction

For every model request, the agent builds context in this order:

1. canonical system instructions and precedence statement;
2. project memory for the current workspace;
3. current runtime and workspace projection;
4. latest valid checkpoint, if present;
5. message events whose sequence is greater than `covers_through_seq`;
6. the current user message, which is already one of those message events.

Runtime-only journal events are never projected as provider messages.

If the estimated input is within `available_input_budget`, no compaction call or
checkpoint write occurs.

If it exceeds the budget, the compactor:

1. groups uncovered messages into atomic interaction groups;
2. treats an assistant tool-call message and all immediately following results
   for its call IDs as one indivisible group;
3. walks groups from newest to oldest to select the largest raw suffix that
   fits the suffix budget and the recent-user budget;
4. sets the new cutoff to the last message-event sequence before that suffix;
5. asks the model to merge the previous checkpoint, if any, with uncovered
   messages through the cutoff into the strict checkpoint schema;
6. validates and atomically persists the checkpoint;
7. rebuilds context as system projection + checkpoint + message events after
   the new `covers_through_seq`;
8. verifies the rebuilt request fits before calling the main model.

The compactor never splits a tool interaction group. If the newest user message
alone exceeds the available input budget, or no valid checkpoint can produce a
request that fits, context construction fails clearly instead of silently
dropping or truncating user text.

## Incremental Compaction

A later compaction does not summarize the entire journal again. Its summary
input is:

```text
previous checkpoint
+ message events from previous covers_through_seq + 1 through new cutoff
```

The replacement checkpoint advances `covers_through_seq`. Message events after
that sequence remain raw. This bounds repeated compaction cost while preserving
the journal as the replay source of truth.

The checkpoint prompt requires JSON only. Invalid JSON, an invalid schema, or a
summary larger than the checkpoint allowance causes compaction to fail without
replacing the previous valid checkpoint.

## Resume Flow

When a session is resumed:

1. open and validate the append-only journal;
2. replay runtime state exactly as today;
3. load the checkpoint for the same session;
4. load `PROJECT.md` for the current workspace identity;
5. construct context from checkpoint plus uncovered message events;
6. compact again only if that reconstructed context exceeds the current model
   budget.

The checkpoint is not used to decide turn, plan, approval, tool, or recovery
state. Those remain reducer outputs from the journal.

## Failure Handling

- Missing project memory creates the standard template lazily.
- A project-memory write failure leaves the previous file intact and reports an
  error to the CLI.
- A missing checkpoint falls back to journal-only construction.
- A corrupt checkpoint is treated as unavailable; runtime replay still uses the
  journal.
- A failed checkpoint generation leaves the previous checkpoint intact and
  fails the model iteration with a visible context-compaction error.
- Journal corruption continues to fail closed under the existing event-store
  rules.
- No failure path falls back to the shared `projects/default/PROJECT.md`.

## Measurement

Every successful compaction appends a `ContextCompacted` journal event with:

- previous and new `covers_through_seq`;
- estimated input tokens before and after compaction;
- summary-input tokens;
- compaction duration in milliseconds.

The reducer treats this as a known runtime no-op. These events make token
reduction, compaction frequency, repeated-summary cost, and latency measurable
without making the checkpoint canonical lifecycle state.

Project cross-contamination is evaluated by tests that create two workspace
identities under the same state root and verify each loads only its own file.

## Acceptance Criteria

The MVP is complete when all of the following are true:

1. Two workspaces under one state root read and write different `PROJECT.md`
   files.
2. No completed turn triggers an automatic project-memory model call.
3. `/memory`, `/remember`, and `/forget` work without entering the agent loop.
4. Context below budget causes no compaction model call or checkpoint write.
5. Context above budget creates a validated checkpoint and retains atomic tool
   interactions.
6. A second compaction summarizes only journal content not covered by the first
   checkpoint.
7. Resuming a session loads its checkpoint and only the uncovered raw messages.
8. Missing or corrupt checkpoints do not damage or rewrite the journal.
9. Oversized current user input fails explicitly without silent truncation.
10. Existing runtime, recovery, plan, sandbox, and journal tests continue to
    pass.
