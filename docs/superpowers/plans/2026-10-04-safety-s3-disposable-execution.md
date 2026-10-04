# Safety S3 Disposable Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Run supported opaque actions offline on read-only sealed inputs and revoke all descendants before returning results.

**Architecture:** Separate file-adapter use from disposable shell containers. Reuse Docker runtime probes and resource controls; verify the exact RO mount/profile, then destroy each action container in a finally path and quarantine uncertain outcomes.

**Tech Stack:** Python >=3.10, stdlib subprocess/unittest, rootless Docker with pinned existing trusted image.

**Spec:** `docs/superpowers/specs/2026-10-04-safety-implementation-design.md`, sections 2, 5.3, 7-8.

**Prerequisite:** S1 and S2; use `Generation`, `SandboxSession.security_context`, `ActionBinding`, and durable one-use grant/checkpoint contracts.

## Global Constraints

- No new product dependencies; no host execution fallback.
- Opaque input generation is entirely RO; RW shell is unsupported and hard DENY.
- Pin image/helper; network none, rootfs RO, non-root user, cap-drop ALL, no-new-privileges.
- Existing CPU/memory/PID/time/output ceilings apply; /tmp has hard size and inode bounds verified physically.
- Stop/remove the action container on success/error/timeout/cancel; process groups alone do not establish quiescence.
- Do not rebuild trusted image from agent-editable source at startup; namespace loopback remains possible.

## Review Focus

- Successful foreground exit leaves a setsid child with closed stdio: child must be dead (Task 2).
- Docker exec succeeds but remove/inspect fails: result remains unknown/quarantined (Task 2).
- Existing session image/helper/mount differs from pinned contract: deny reconciliation (Task 1).
- Project test writes a cache/control file: fail on RO input without silently granting RW (Task 3).
- Tmpfs inode exhaustion differs from byte exhaustion: verify both actual bounds (Task 1).

## Task 1: Explicit RO action profile and trusted startup

**Files:** Modify `src/sandbox/docker.py`, `src/sandbox/models.py`, `sandbox-image/Dockerfile`, `docker-entrypoint.sh`, `compose.yaml`; update `tests/test_docker_backend.py`, `tests/test_compose_launcher.py`, `docs/run_guide.md`.

**Interfaces:** `DockerBackend.create_action(action_id: str, generation: Generation, limits: ResourceLimits) -> str` builds a new independently managed RO container with controller-generated action labels; separate from legacy `create`. `verify_action(limits: ResourceLimits, generation: Generation) -> dict` checks actual inspect/probe evidence, not submitted argv. Preserve pin format validation and `--pull never`. Add verified tmpfs inode budget to ResourceLimits; image uses fixed trusted entrypoint/env. Startup takes SANDBOX_IMAGE or explicit `--image`; no docker build.

- [x] Write `DockerActionTests.test_ro_profile_and_no_credentials`: inspect/probe matches RO generation mount, pinned image, non-root/no-capabilities/network/resource controls; unrelated mounts/env/socket/FDs absent. Any mismatch denies before execution.
- [x] Write `test_tmpfs_budgets_and_bad_image`: explicit byte/inode settings required; unverified quota rejects profile. Mutable tag or changed digest/helper identity does not reuse approval. Shell input is never source_workspace.
- [x] Write `ComposeLauncherTests.test_pinned_image_no_auto_build`: launcher never builds sandbox from project; missing digest fails with clear setup instruction. Trusted control container credentials/socket never enter action container.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_docker_backend tests.test_compose_launcher -v`, observe new failures; implement profile/startup changes and rerun until pass.

## Task 2: Lifetime revocation, bounded output and unknown outcomes

**Files:** Modify `src/sandbox/docker.py`, `src/sandbox/session.py`, `src/sandbox/models.py`, `sandbox-image/sandbox_exec.py`, `src/tools/terminal.py`; update `tests/test_sandbox_session.py`, `tests/test_sandbox_helpers.py`, `tests/test_sandbox_tools.py`.

**Interfaces:** `DockerBackend.exec_action(request: ExecRequest, generation: Generation, limits: ResourceLimits) -> SandboxResult` owns create/start/verify/exec/terminate/remove/verify-dead lifecycle. `SandboxSession.exec` calls it only for a verified S1 grant. ExecRequest carries controller execution ID; remove the fixed `tool` action identity. SandboxResult adds `process_outcome`, `enforcement_observation`, `effect_state`, `quiescent`, retaining existing result fields for journal migration.

- [x] Write `ActionLifetimeTests.test_all_finally_paths`: normal exit, nonzero, timeout, cancel, malformed JSON and lost transport all attempt whole-container revocation; no result is completed before verified quiescence. Stop/inspect/remove failure makes session ERROR/quarantined and prevents next action.
- [x] Write `test_output_and_timeout_budget`: helper honors configured output_bytes rather than fixed 10 MiB; controller bounds transport read/JSON payload too; timeout cleanup handles already-exited foreground safely.
- [x] Write `test_outcome_not_safety`: exit zero does not imply NOT_DETECTED; transport/audit ambiguity produces UNKNOWN and no retry/publication. No stdout value can impersonate trusted metadata.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_sandbox_session tests.test_sandbox_helpers tests.test_sandbox_tools tests.test_safety_execution -v`, observe new failures; implement lifecycle/reports and rerun until pass.

## Task 3: Physical acceptance and usable RO workflows

**Files:** Modify `tests/test_sandbox_integration.py`, `docs/sandbox_v1_operations.md`, `docs/run_guide.md`.

**Interfaces:** Integration tests consume the Task 1/2 profile directly and record actual image/daemon/profile evidence. No changes to capability scope during test failure.

- [x] Write physical tests: success spawns a setsid child that closes stdio; after action completion inspect/remove evidence and host-observed process markers show no survivor. Repeat timeout/cancel and reject failed cleanup.
- [x] Write physical tests for RO source/control/absent-path and ancestor mutation, external/host/metadata connections, namespace-local listener behavior, cgroup limits and tmpfs byte/inode exhaustion. Ensure original dirty/untracked source remains byte/mode/inode unchanged.
- [x] Write a benign workflow test: adapter edits main.py, seal generation, run Python with `PYTHONDONTWRITEBYTECODE=1` to exercise changed behavior; cache-writing test fails explicitly on RO scope without escalation to unsupported RW.
- [x] Run `RUN_SANDBOX_INTEGRATION=1 SANDBOX_IMAGE=<pinned-digest> PYTHONPATH=src .venv/bin/python -m unittest tests.test_sandbox_integration -v`. Require physical cases to execute/pass; skips leave the enforcement gate unmet.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` and `git diff --check`; document supported RO workflow and remaining RW limitations.

## Completion and handoff

Record exact test/probe evidence; do not mark container mocks as physical verification. Proceed to `2026-10-04-safety-s4-disclosure-export.md` only after gates pass. Stage only explicit task files if committing.

## Implementation record (2026-10-04)

Supported scope is implemented in the separate code-safety worktree.
Checklist coverage includes equivalent or combined tests where final test names
differ from the proposed names. See `../safety-acceptance.md` for current
evidence, review fixes, unsupported capabilities and deferred declassification.
Final discovery: 329 tests OK (11 opt-in physical skips). Separate rootless
physical run: 11 tests OK, zero skips. Independent review could not run
because the reviewer provider had no active credentials; author review
and regression verification are recorded explicitly. No commit or merge.
