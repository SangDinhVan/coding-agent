# Project Memory Review Design

> Status: approved in-chat design, written spec awaiting user review.
>
> This spec replaces manual `/remember` writes with LLM-proposed, user-approved
> project-memory updates after completed work and at graceful session exit.

## Goal

Project memory should capture durable, reusable facts without requiring the
user to remember a command and without allowing the model to write directly to
`PROJECT.md`.

The lifecycle is:

```text
completed work or graceful session exit
        -> extract candidates with the LLM
        -> validate provenance and remove duplicates
        -> ask the user which candidates to save
        -> persist only approved facts
```

Compaction remains independent. It preserves one session's continuity; it does
not update project memory.

## Scope

This change includes:

- removing the `/remember` REPL command;
- keeping `/memory` and `/forget`;
- proposing memory candidates after each completed turn;
- reviewing any completed but unreviewed work at graceful session exit;
- requiring user approval before every project-memory write;
- requiring every candidate to cite journal event sequences;
- persisting a journal cursor so reviewed work is not proposed repeatedly;
- treating extraction, validation, and approval failures as non-fatal.

This change does not include:

- automatic writes without user approval;
- memory extraction during compaction;
- vector search, embeddings, or relevance retrieval;
- promotion of failed, interrupted, or active-turn state;
- a claim that `TurnCompleted` proves tests passed;
- cross-session consolidation beyond existing exact-fact deduplication.

## Current Behavior

The current REPL exposes:

```text
/memory
/remember <fact>
/forget <id-or-fact>
```

`/remember` passes user text directly to `MemoryManager.remember()`. No LLM is
involved. The manager normalizes whitespace, performs exact case-insensitive
deduplication, assigns an ID, and writes atomically.

The internal `MemoryManager.remember()` method remains useful as the final
write primitive. Only the public command is removed.

## Trigger Semantics

### Completed-turn trigger

After `TurnCompleted` is durable, the REPL requests memory candidates for the
newly completed, not-yet-reviewed event range.

`TurnCompleted` means the runtime accepted the turn as complete. It does not by
itself mean tests or other verification ran. Verification claims must cite
successful tool evidence.

If extraction returns no valid new candidates, the review range is still
marked complete so the same work is not proposed again.

### Session-exit trigger

Before a graceful REPL exit, the same review pipeline processes completed
events after the latest durable review cursor. This catches work whose normal
post-turn review did not run or did not finish.

The interactive exit review runs for explicit `exit` and `quit`, while the
input channel is still available for approval. EOF and top-level user
interruption close without review because the system can no longer reliably
collect approval. Memory review must never prevent exit. An interruption
during review skips the review and closes the agent.

The exit review stops at the latest `TurnCompleted` event. It excludes active,
failed, and interrupted turns.

`Agent.close()` remains deterministic and does not call the model. The REPL
owns the graceful-exit review before invoking `close()`.

## Review Cursor

The journal records a no-op runtime event after a review decision:

```json
{
  "event_type": "MemoryReviewCompleted",
  "aggregate_type": "memory_review",
  "payload": {
    "trigger": "turn_completed",
    "reviewed_from_seq": 101,
    "reviewed_through_seq": 148,
    "accepted_memory_ids": ["a1b2c3d4"],
    "rejected_count": 1
  }
}
```

`trigger` is `turn_completed` or `session_exit`. The greatest
`reviewed_through_seq` is the durable cursor for the next review.

An empty candidate set and an explicit user rejection both advance the cursor.
An extraction or persistence failure records `MemoryReviewFailed` but does not
advance it, allowing a later exit or resumed session to retry.

Both event types are accepted as known no-op events by runtime replay. They do
not modify turn, plan, or tool state.

`MemoryReviewFailed` stores `trigger`, `attempted_from_seq`,
`attempted_through_seq`, and a structured error category/message. It never
changes the successful review cursor.

If no completed event range exists after the cursor, neither trigger calls the
model or prompts the user.

## Component Boundaries

The agent owns extraction and validation because it already owns the journal,
runtime state, model configuration, and `MemoryManager`.

The REPL owns interactive approval because it already owns `input_fn` and
`print_fn`. Approval is passed back to the agent as selected candidate indexes;
the agent persists accepted facts and writes the review event.

The extractor uses the currently configured model through `llm.complete_text`.
This change does not add a second memory-specific model setting.

## Extraction Input

The extractor does not read the entire session journal. It receives only:

1. the current `PROJECT.md` content;
2. eligible events after the latest review cursor through the selected
   completed-turn boundary;
3. compact tool results and tool metadata, not raw tool output;
4. the relevant user messages and final assistant response;
5. explicit instructions that project memory is advisory and must contain only
   durable cross-session facts.

Runtime lifecycle noise unrelated to evidence is omitted from the prompt.
Failed and cancelled tool executions may provide context but cannot serve as
verification evidence.

The extraction prompt is counted with the existing `model.llm` token helper and
must fit the current model's available input budget, including the existing
output and safety reserves. This change does not add chunked memory extraction.
If the bounded range does not fit, extraction fails non-fatally rather than
truncating an event or silently accepting unsupported candidates.

## Candidate Schema

The LLM returns JSON only:

```json
{
  "candidates": [
    {
      "fact": "Repository runs unit tests with pytest.",
      "kind": "verified_project_fact",
      "source_seqs": [132, 139]
    },
    {
      "fact": "The public authentication API must remain backward compatible.",
      "kind": "user_constraint",
      "source_seqs": [101]
    }
  ]
}
```

The exact allowed keys are `fact`, `kind`, and `source_seqs`.

Allowed kinds:

- `verified_project_fact`: a reusable project fact supported by at least one
  successful `ToolCompleted` or `ToolRecoveredAsCompleted` event;
- `user_constraint`: a durable constraint stated by the user in a cited
  `UserMessageRecorded` event.

Validation rejects a candidate when:

- the response is not valid JSON or does not match the exact schema;
- `fact` is empty or exceeds 500 characters after whitespace normalization;
- `kind` is unknown;
- `source_seqs` is empty, duplicated, outside the reviewed range, or references
  missing events;
- the cited event types do not support the selected kind;
- the fact exactly matches an existing managed memory, case-insensitively.

Schema and provenance validation establish traceability, not semantic truth.
The user approval remains the final authority.

## Approval Flow

When valid candidates remain, the REPL displays them as one numbered batch:

```text
Proposed project memories:

1. Repository runs unit tests with pytest.
2. The public authentication API must remain backward compatible.

Save memories (all / 1,2 / none):
```

Accepted input is:

- `all` to accept every candidate;
- `none` or an empty response to reject all;
- a comma-separated list of candidate numbers.

Invalid selections are requested again. Each accepted fact is persisted with
the existing `MemoryManager.remember()` method, so exact duplicates remain
idempotent and writes retain the existing lock and atomic-replace behavior.

The user approves facts, not model-written instructions. Candidate text is
stored only in the managed `Curated Memories` section.

## Failure Behavior

Memory review is secondary to task execution and shutdown:

- extraction API failure prints a concise warning and leaves the cursor
  unchanged;
- invalid JSON or invalid provenance is rejected without writing memory;
- one invalid candidate does not require accepting the rest of the response;
- persistence failure reports the error and records no successful cursor past
  facts that were not written;
- user rejection is a successful review and advances the cursor;
- review failure never changes `TurnCompleted` into `TurnFailed`;
- review failure never prevents graceful session exit.

For a partial persistence failure, the review event records only IDs that were
actually returned by successful `remember()` calls. The cursor does not advance
past the range, so a later retry may re-propose facts; existing accepted facts
are removed by deduplication.

## Command Surface

After this change:

```text
/memory
/forget <id-or-exact-fact>
```

`/remember` is no longer recognized as a command and is not sent to the model.
The REPL prints a short message explaining that memory is proposed after
completed work and at session exit.

`/forget` remains necessary because user approval does not guarantee a fact
will stay correct forever.

## Testing

The implementation must cover:

- `/remember` is removed while `/memory` and `/forget` still work;
- completed turns invoke one review for the correct sequence range;
- no candidates advances the review cursor without prompting;
- valid candidates display one batch approval prompt;
- `all`, `none`, and selected indices persist the expected facts;
- invalid selection retries without writing;
- duplicate existing facts are not proposed;
- unsupported event provenance rejects a candidate;
- model-authored statements without user/tool evidence are rejected;
- failed, interrupted, and active turns are excluded from exit review;
- session exit reviews only events after the latest cursor;
- accepted and rejected reviews are not proposed again after resume;
- extraction and persistence failures do not fail the turn or block exit;
- memory-review events replay as no-ops;
- the extractor never includes raw tool output in its prompt.

## Documentation

`docs/memory.md` must be updated to remove `/remember` from the recommended
flow and describe completed-turn/session-exit candidate review as the project
memory update policy.

The existing memory/context MVP spec remains historical documentation of the
previous explicit-command behavior. This spec supersedes only its project
memory update and command-surface sections.
