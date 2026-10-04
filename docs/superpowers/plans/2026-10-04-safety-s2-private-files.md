# Safety S2 Private Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Admit independent private data, seal generations and enforce bounded, exact file effects with durable before-images.

**Architecture:** Harden the existing snapshot and descriptor-relative helper instead of replacing them. Keep sealed generations outside untrusted mounts; supply S1 with verified controller facts and narrowly scoped one-use file grants.

**Tech Stack:** Python >=3.10, stdlib filesystem/hash/JSON/unittest, existing Docker backend.

**Spec:** `docs/superpowers/specs/2026-10-04-safety-implementation-design.md`, sections 2, 5.1-5.2, 8.

**Prerequisite:** S1 control-plane plan passes; consume its `AuthorityRecord`, `ActionBinding` and `ToolGrantClaimed` contracts.

## Global Constraints

- No new product dependencies; safe workspace is SHADOW; LIVE resume is rejected.
- All source copies use independent private inodes; no symlink/special/multi-hardlink admission.
- No arbitrary writable shell. File grants bind exact operation/path/content and before-state.
- Controller/private generations/state are excluded from every untrusted mount.
- Retain existing resource ceilings; enforce adapter limits before effects. Watchdog is not a hard quota.

## Review Focus

- Source directory changes into a symlink during copy: no outside reads (Task 1).
- State root is ancestor or descendant of source: reject before mkdir/chmod (Task 1).
- Parent/target replacement or target absent at review: do not redirect approved write (Task 3).
- Same content with changed type/mode: binding/checkpoint must distinguish it (Task 2).
- Admission fails partway: no partial generation may become executable (Task 2).

## Task 1: Source admission and safe defaults

**Files:** Modify `src/core/paths.py`, `src/sandbox/workspace.py`, `src/sandbox/session.py`, `src/main.py`; update `tests/test_workspace_snapshot.py`, `tests/test_sandbox_session.py`, `tests/test_main.py`.

**Interfaces:** Preserve `ControlPaths.create` and `prepare_workspace` signatures; add optional `max_entries=10000` to admission. `SandboxSession` default becomes SHADOW. Validate overlap in both directions before state directory creation. Safe CLI rejects persisted LIVE metadata with `unsupported_profile`. Repository excludes remain monotonic restrictions.

- [ ] Write `SnapshotAdmissionTests.test_overlap_has_no_side_effect`: root equals source, root below source and root containing source all fail before creating/chmodding paths; disjoint roots pass.
- [ ] Write `test_inode_and_source_races`: internal/external symlinks, special files, regular files with st_nlink > 1 reject; replace a parent/leaf during injected copy pause and assert no outside bytes admitted. For stable dirty/untracked files assert distinct source/private inode and byte/mode equality.
- [ ] Write `test_bounded_admission`: 10001 admitted entries or existing baseline byte ceiling exceeded fails; private exclusions win over `.agentignore`/`.gitignore`; no hook/filter command executes.
- [ ] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_workspace_snapshot tests.test_sandbox_session tests.test_main -v`, observe new failures; implement no-follow descriptor copy plus before/after identity checks and defaults; rerun until pass.

## Task 2: Sealed generations and checkpoint contract

**Files:** Create `src/sandbox/generations.py`, `tests/test_generations.py`; modify `src/core/paths.py`, `src/sandbox/session.py`, `src/runtime/executor.py`, `src/sandbox/models.py`.

**Interfaces:** Frozen `Generation(digest: str, directory: Path, manifest: tuple[ManifestEntry, ...])`; `seal_generation(workspace: Path, destination_root: Path, *, max_bytes: int, max_entries: int) -> Generation`; `verify_generation(generation: Generation) -> bool`. Extend ControlPaths with private `generations` and `checkpoints`. `SandboxSession.security_context() -> dict` returns S1 generation/image/env/scope/authority facts only when verified; `checkpoint_action(action: NormalizedAction) -> str` durably stores before-content/type/mode or absence and returns its digest. File mutation invalidates current generation; seal a new one before later review/execution.

- [ ] Write `GenerationTests.test_private_seal_and_binding`: sealed inode independent of mutable working copy, sandbox cannot mount/write sealed directory, identical manifests have equal digest, content/type/mode changes differ. Include missing targets in checkpoint.
- [ ] Write `test_failed_or_changed_seal_never_current`: mid-copy failure, source race, symlink/special inode and budget violations leave no executable current generation. Revalidation rejects hash mismatch.
- [ ] Write `test_checkpoint_fsync_failure_blocks_tool`: unavailable before-image/manifest/directory fsync prevents ToolGrantClaimed/ToolStarted and all writes; success persists recoverable dirty/untracked bytes without reliance on Git.
- [ ] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_generations tests.test_safety_execution -v`, observe failure; implement private generation/checkpoint integration; rerun until pass.

## Task 3: Exact filesystem grants and bounded mutations

**Files:** Modify `sandbox-image/sandbox_fs.py`, `src/sandbox/session.py`, `src/sandbox/docker.py`, `src/tools/filesystem.py`; update `tests/test_sandbox_helpers.py`, `tests/test_sandbox_tools.py`, `tests/test_sandbox_integration.py`.

**Interfaces:** Controller creates `FileGrant(operation, path, content_digest, before_digest, max_bytes, max_entries, protected_diff_digest)` and supplies it out of band to the trusted helper invocation; never accept a grant field from model kwargs. Existing helper `handle(request, workspace=...)` gets a separate `grant` parameter. Actual read/write/edit and fingerprint operations must match it; grants claim once through S1. `SandboxSession.fs_call(request)` remains the tool API and receives only controller-approved requests; `fingerprint` is a controller read within authority, not an approval bypass.

- [ ] Write `FileGrantTests.test_exact_request_and_control_diff`: ordinary scoped read/edit works; forged/missing grant, other path/op/content, or changed before-state blocks; approved protected exact diff works only through adapter and never authorizes shell.
- [ ] Write `test_absent_target_and_parent_replacement`: approve new nested path, replace parent/leaf with symlink before write, assert no outside effect; preserve type/mode/before evidence in binding. No unapproved ancestor removal/recreation helper operation exists.
- [ ] Write `test_bounded_atomic_write`: oversized read/write/edit, aggregate bytes/entries ceiling, and partial os.write fail safely; successful replacement persists full content and directory fsync; before-images survive failure.
- [ ] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_sandbox_helpers tests.test_sandbox_tools tests.test_safety_policy -v`, observe new failures; extend no-follow helper and controller invocation; rerun until pass.
- [ ] With a verified rootless daemon and independently built pinned helper image, run `RUN_SANDBOX_INTEGRATION=1 SANDBOX_IMAGE=<pinned-digest> PYTHONPATH=src .venv/bin/python -m unittest tests.test_sandbox_integration -v`; require real file grants/limits/isolation tests to pass. Record an unavailable daemon/image as an unmet gate, not a pass.

## Completion and handoff

Run the full unittest suite and `git diff --check`. Record exact physical adapter evidence and keep unsupported profiles DENY. S2 enables ordinary private read/edit only; proceed to `2026-10-04-safety-s3-disposable-execution.md`. Stage only explicit task files if committing.
