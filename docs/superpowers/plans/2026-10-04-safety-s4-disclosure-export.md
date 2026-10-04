# Safety S4 Disclosure And Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Preserve security provenance, gate every disclosure sink and export a sealed patch without writing to the original project.

**Architecture:** Extend existing security/journal projections and put a shared disclosure gate ahead of text sinks. Reuse change-set sealing with immutable private inputs and a descriptor-relative export broker; safe CLI removes automatic host apply.

**Tech Stack:** Python >=3.10, stdlib difflib/hash/JSON/unittest, existing context/memory and model client.

**Spec:** `docs/superpowers/specs/2026-10-04-safety-implementation-design.md`, sections 2, 6-8; integration acceptance inherits `docs/safety.md` sections 5 and 7.

**Prerequisite:** S1-S3; use S1 effective authority/bindings and S2 sealed generations, and require S3 quiescence before harvest/export.

## Global Constraints

- No new product dependencies; use existing journal and memory/context storage.
- Sink authorization is separate from filesystem authority; secret exposure blocks disclosure.
- Screening is supplementary, cannot prove absence of unknown/encoded secrets, and never creates authority.
- Keep raw data bounded/quarantined until sink gate evaluation; traces stay private.
- Sealed patch export only; safe CLI exposes no automatic host apply or recovery that overwrites source.
- No external broker, standing approval, SLM, RW shell, or automatic publication in this release.

## Review Focus

- Output looks like trusted metadata/instructions: controller keeps it tainted (Task 1).
- Secret appears in provider exception, tool arguments or diff: gate before any sink (Task 2).
- Summary/restart removes original evidence: replay retains security version/provenance (Task 1).
- Output destination parent changes to symlink after approval: export cannot escape (Task 3).
- Export is requested while termination/harvest/audit is unknown: no artifact release (Task 3).

## Task 1: Persistent monotonic security state

**Files:** Modify `src/runtime/models.py`, `src/runtime/reducer.py`, `src/runtime/safety.py`, `src/runtime/executor.py`, `src/agent/loop.py`, `src/context/checkpoint.py`, `src/context/compactor.py`, `src/memory/manager.py`; create `tests/test_security_state.py`; update `tests/test_checkpoint.py`, `tests/test_compactor.py`, `tests/test_memory_manager.py`.

**Interfaces:** `SecurityState` contains untrusted_content_seen, agent_modified_content, injection_flags, secret_exposure_detected, provenance_generation_ids and security_state_version. `SecurityStateUpdated` journal event records controller annotations and generation IDs, not stdout-provided labels. Replay merges flags monotonically; only explicit controller declassification may narrow them. Existing CompactionCheckpoint and memory facts carry provenance/security version with legacy defaults, without making checkpoints a source of authority. `screen_artifact(content: str, generation_id: str) -> dict` returns bounded annotations.

- [x] Write `SecurityStateTests.test_same_batch_update`: output flags/security version applied before next action policy/Reviewer and before tool message append; forged JSON/status from stdout cannot clear flags or create trusted metadata.
- [x] Write `test_compact_and_restart_keep_provenance`: read -> edit/copy -> compact -> close/resume retains generation IDs/injection/secret flags; summary text never acts as authority and old checkpoints cannot reset current security state.
- [ ] Write `test_declassification_is_control_plane_only`: tool/repo/memory edits cannot clear flags; authenticated user action has its own journal event/revision and invalidates pending bindings. **Deferred:** clearing persisted flags was rejected by automatic approval review; this release has no declassification API and flags remain monotonic.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_security_state tests.test_checkpoint tests.test_compactor tests.test_memory_manager -v`, observe new failures; implement state/provenance integration and rerun until pass.

## Task 2: Pre-sink disclosure and private tracing

**Files:** Modify `src/runtime/safety.py`, `src/runtime/executor.py`, `src/memory/event_store.py`, `src/memory/manager.py`, `src/model/llm.py`, `src/context/compactor.py`, `src/agent/loop.py`, `src/main.py`; create `tests/test_disclosure.py`; update `tests/test_model_llm.py`, `tests/test_agent_loop.py`, `tests/test_event_store.py`.

**Interfaces:** `DisclosureGate.check(content: str, sink: str, authority: AuthorityRecord, security_state: SecurityState, provenance: tuple[str, ...]) -> str` returns gated text or raises controller-owned `DisclosureDenied(reason_code)`. Sink IDs are main_model, reviewer, summarizer, memory, ui, audit_text, trace, patch_export. Session gate is passed by trusted setup; an OS tool cannot select or disable it. Raw quarantine is private, bounded and never included in normal message projections. JSON redaction remains defense in depth, not the authorization gate.

- [x] Write `DisclosureTests.test_each_sink_before_side_effect`: known secret canary in read/stdout/arguments/error/diff stops provider calls, UI printing, summary/memory writes, raw journal text, trace and patch output. Missing authority/provenance or gate exception also blocks; safe controller reason metadata remains available.
- [x] Write `test_streaming_and_trace_do_not_disclose_early`: inspect/gate full outgoing request before provider call; model output requires bounded gated handling before UI disclosure; trace request/exception data is gated before append. Do not copy .llm-traces into source at turn end.
- [x] Write `test_reviewer_uses_structural_analysis`: tainted code/stdout is absent from Reviewer prompt; script/import generation with incomplete structural analysis routes to sealed-content human review rather than raw-code Reviewer fallback.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_disclosure tests.test_model_llm tests.test_event_store tests.test_agent_loop -v`, observe new failures; wire gate at every named sink and rerun until pass.

## Task 3: Bounded harvest and digest-bound patch export

**Files:** Modify `src/sandbox/changes.py`, `src/sandbox/session.py`, `src/agent/loop.py`, `src/main.py`; create `tests/test_patch_export.py`; update `tests/test_changeset.py`, `tests/test_main.py`, `tests/test_sandbox_integration.py`.

**Interfaces:** `build_changeset` reads S2 immutable before/current generations and requires S3 quiescence evidence. `export_patch(paths: ControlPaths, changes: ChangeSet, approved_digest: str, export_root: Path, relative_name: str, disclosure_gate: DisclosureGate) -> Path` uses an authorized root and no-follow descriptor walk. CLI `/export <relative-name>` requires an explicit export sink authority; `/changes` previews gated exact content. `/apply` returns `publication_unavailable` in the safe profile. `Agent.apply_changes` cannot be reached from safe tool/model flow. Internal legacy apply/recovery code remains unavailable to the safe CLI until separately redesigned.

- [x] Write `PatchExportTests.test_exact_sealed_patch_and_original_untouched`: export digest matches reviewed changes and immutable before/after bytes, including newline/create/delete/mode representation; original dirty/untracked tree remains unchanged. Changed/incorrect digest denies.
- [x] Write `test_bounded_harvest`: links/special/unsafe modes, over 10000 entries, per-file 20 MiB/aggregate 100 MiB or harvest deadline exceeded deny; unchanged huge files/empty directories also count toward scan budget. Require quiescence before scan. Binary/non-UTF8 unsupported patch content fails explicitly without lossy conversion.
- [x] Write `test_export_path_races`: absent sink, symlink leaf/parent replacement, traversal/absolute names and denied disclosure never write outside export authority; destination conflict does not overwrite user files. No Git hook/filter/build executes during sealing or export.
- [x] Write `test_unknown_effect_and_recovery_conflict`: uncertain stop/transport/post-effects audit blocks export/mutation; private recovery checks expected-after type/mode/hash plus writer exclusion, keeps conflicting bytes/backups and reports recovery_required. No automatic host restore on safe startup.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_patch_export tests.test_changeset tests.test_main tests.test_recovery -v`, observe new failures; implement bounded generation scan/export and CLI removal of automatic apply; rerun until pass.

## Task 4: Complete acceptance and operating instructions

**Files:** Modify `tests/test_sandbox_integration.py`, `docs/run_guide.md`, `docs/sandbox_v1_operations.md`, `docs/superpowers/specs/2026-10-04-safety-implementation-design.md`; create `docs/superpowers/safety-acceptance.md`.

**Interfaces:** Acceptance ledger maps every safety.md section 7 row to its test, latest result, supported profile and residual limitation. Mark unsupported future capability explicitly; do not label it implemented because DENY prevented execution.

- [x] Add end-to-end case: admitted dirty/untracked source -> private read/edit -> required approval -> content-bound RO execution -> gated sealed patch export -> source intact. Exercise ask_all/ask_on_escalation/auto_review and headless refusal at appropriate gates.
- [x] Add injection chain case: README instruction -> proposed dangerous action -> mode routing/scope enforcement -> compact/resume -> later action; assert original source/private state/credentials never enter an untrusted mount and taint persists.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q`; require no failures. Run physical integration with `RUN_SANDBOX_INTEGRATION=1 SANDBOX_IMAGE=<pinned-digest> PYTHONPATH=src .venv/bin/python -m unittest tests.test_sandbox_integration -v`; require actual execution of the supported profile cases before claiming it verified.
- [x] Run `git diff --check`; review authorization/replay/enforcement/disclosure/export boundaries against safety.md. Populate acceptance ledger with observed evidence; list missing physical environment as an unmet verification gate if applicable.
- [x] Update run guide with trusted image setup, source/provider/export authority, three modes, headless command, RO test limitations, `/changes`/`/export`, and unknown/recovery handling. Mark spec implemented only to the extent supported by ledger evidence.

## Completion

Report implemented milestones, test counts/skips, real Docker evidence and remaining safety.md gaps. Preserve user changes. Stage only explicit task files if committing. Automatic publication and RW shell need their own design, policy, physical acceptance and human approval; do not enable them to make an unsupported test pass.

## Implementation record (2026-10-04)

Supported scope is implemented in the separate code-safety worktree.
Checklist coverage includes equivalent or combined tests where final test names
differ from the proposed names. See `../safety-acceptance.md` for current
evidence, review fixes, unsupported capabilities and deferred declassification.
Final discovery: 329 tests OK (11 opt-in physical skips). Separate rootless
physical run: 11 tests OK, zero skips. Independent review could not run
because the reviewer provider had no active credentials; author review
and regression verification are recorded explicitly. No commit or merge.
