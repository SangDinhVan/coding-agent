# Safety S1 Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Enforce the approval matrix, immutable review bindings and fail-closed execution orchestration in the existing runtime.

**Architecture:** Extend the existing journal/reducer/executor; a small safety module holds controller records and routing. Backend facts are mandatory for OS tools, so this milestone does not falsely certify the current Docker profile.

**Tech Stack:** Python >=3.10, stdlib unittest, existing OpenAI SDK.

**Spec:** `docs/superpowers/specs/2026-10-04-safety-implementation-design.md`, sections 1-4, 7-8.

## Global Constraints

- No new product dependencies.
- `ask_on_escalation` default; `interactive` default; legacy ASK means mandatory human approval.
- Missing policy/authority/backend facts deny before any human or Reviewer call.
- Reviewer timeout 30 seconds, no tools, no streaming, no retries.
- SHADOW is the safe profile; do not advertise S1 as complete isolation.
- Execution order: S1 -> S2 -> S3 -> S4. Native execution is recommended because the tasks share the controller and reducer.

## Review Focus

- Approved callback action changes before effects: recheck must block it (Task 3).
- Restart with legacy unbound approval: require fresh review (Task 3).
- Valid JSON with wrong types/extra keys: reject before policy/effects (Task 2).
- Provider returns tools or arbitrary rationale instead of schema: never auto-allow (Task 4).
- Second approval arises while draining a batch: resolve without a new model/user turn (Task 5).

## Task 1: Configuration and decision routing

**Files:** Create `src/runtime/safety.py`, `tests/test_safety.py`; modify `src/runtime/models.py`.

**Interfaces:** Define frozen `SafetyConfig(approval_mode, interaction_mode, policy_revision, allowed_modes, pinned_mode, profile_id, constraints_digest)`, `PolicyResult(decision, reason_code, mandatory_human_approval=False, binding=None)`, and `ReviewVerdict(decision, reason_code)`. Add `ApprovalMode` and `InteractionMode` enums, and `PolicyDecision.REVIEW`. `route_approval(result: PolicyResult, config: SafetyConfig, verdict: ReviewVerdict | None = None) -> str` returns `deny`, `human`, `reviewer`, or `execute`. Legacy ASK sets a mandatory human gate. Configuration validation precedes routing.

`load_safety_config(host_path: Path | None, repo_path: Path | None, requested_mode: str | None, interaction_mode: str, retained_constraints: dict) -> SafetyConfig` reads bounded JSON. Optional `--host-policy` must point outside admitted/source scope; default trusted policy is controller-owned. Host keys are allowed_modes, default_mode, pinned_mode, profile_id, excluded_paths and human_gate_paths. Repository `.agent-safety.json` accepts only excluded_paths and human_gate_paths; all other keys reject. Read at most 64 KiB and reject links/duplicate JSON keys. Merge repository restrictions with journal-retained restrictions by union; revision/digest covers effective restrictions. Deleting the file never clears retained constraints.

- [x] Write `SafetyRoutingTests.test_matrix`: enumerate all 3 modes, both interaction modes, ALLOW/REVIEW/ASK/DENY, mandatory flag, and Reviewer allow/ask/deny; assert the exact spec matrix. Assert hard DENY never returns human/reviewer, headless human routes to deny, and legacy ASK is never reviewer.
- [x] Write `test_disallowed_mode_and_invalid_configuration`: forbidden/pinned mismatch has `approval_mode_not_permitted`; invalid enum/empty revisions has `invalid_approval_configuration`; no fallback.
- [x] Write `test_restrictions_are_monotonic`: repository mode/scope/host-pin fields reject; adding a gate/exclude restricts, removing the file does not relax; an oversized/duplicate-key/symlink config rejects. Host policy inside source is not trusted.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_safety -v`; confirm the new contracts fail before implementation.
- [x] Implement the enums, records and pure router, then run the same command and confirm all cases pass.

## Task 2: Exact normalization and supported-scope policy

**Files:** Modify `src/runtime/safety.py`, `src/tools/base.py`; create `tests/test_safety_policy.py`.

**Interfaces:** `normalize_action(execution: ToolExecution, tool: BaseTool, context: dict) -> NormalizedAction`; `evaluate_policy(action: NormalizedAction, authority: AuthorityRecord, config: SafetyConfig, capabilities: dict, security_state: dict) -> PolicyResult`. `AuthorityRecord` contains controller-generated session/source selection/scope/sink identities. `NormalizedAction` contains canonical arguments SHA-256, path/cwd, generation SHA-256, opaque/egress/deletion facts and execution/session/tool identities. Context is controller input, never tool arguments. `update_plan` is schema-checked internal state only; other unregistered tools deny.

Normalizer input ceiling is 32 MiB (the existing helper request ceiling); host/repo configuration ceiling is 64 KiB. Filesystem content and action generation ceilings are S2 budgets and must be verified separately. Synthetic tools in orchestration tests receive explicit trusted fake context/capabilities; do not add a production bypass to preserve default-ALLOW tests.

- [x] Write `SafetyPolicyTests.test_json_and_schema_boundary`: list/scalar/null args, unexpected fields, wrong string types, bad verification enum, absolute/traversal/noncanonical path all reject; canonical JSON key ordering has equal digest and changed content differs.
- [x] Write `test_capabilities_and_authority_required`: missing policy context/generation/authority/profile evidence denies; ordinary scoped file action allows; supported opaque execution returns REVIEW; unsupported RW shell and external actions deny even if a callback would approve.
- [x] Write `test_protected_control_content`: package scripts, CI, Makefile, tasks and conftest edits have human flag bound to exact before/after content; effective trusted state/runtime targets deny. Use explicit controller path roles, not a filename match as proof of authority.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_safety_policy -v`, observe failure; implement strict validation/normalization and fail-closed policy; run again and confirm pass.

## Task 3: Durable authorization, revalidation and replay

**Files:** Modify `src/runtime/executor.py`, `src/runtime/models.py`, `src/runtime/reducer.py`; create `tests/test_safety_execution.py`; update `tests/test_tool_lifecycle.py`, `tests/test_recovery.py`.

**Interfaces:** Extend `ToolExecutor.__init__` with `safety_config`, `get_security_context`, and `reviewer` keyword inputs. Policy callbacks may return enum/string or `PolicyResult`; exceptions/absent callbacks deny. Define `ActionBinding` with every identity from spec section 4.4; `ToolAuthorizationEvaluated` persists its digest and safe decision facts. Approval metadata/request records carry `binding_digest`. `ToolGrantClaimed` records controller-only one-use claim; replay exposes authorization and claim on `ToolExecution`. Preserve existing `execute`, `resolve_approval`, and recovery signatures.

- [x] Write `SafetyExecutionTests.test_policy_is_mandatory`: no callback or raising callback has zero tool calls and structured denial. Existing fakes explicitly inject ALLOW only where no security behavior is being tested.
- [x] Write `test_approval_rechecks_for_callback_and_resume`: policy flips to DENY or generation/args/modes/security revisions change between presentation and execution; old approval cannot cause effects. Same binding in ask_all executes once without asking twice.
- [x] Write `test_unbound_legacy_approval_and_one_use`: replay of an old approved event re-gates; repeated execution of completed/claimed IDs has zero additional calls; restart after grant claim requires recovery, not automatic retry.
- [x] Write `test_checkpoint_or_intent_failure_has_no_effects`: unavailable checkpoint or failing durable claim/intent has zero tool calls. Post-effect journal failure yields unresolved recovery/quarantine, never retry.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_safety_execution tests.test_recovery tests.test_tool_lifecycle -v` and observe new failures.
- [x] Implement normalization -> hard policy -> routing -> review -> hard recheck/binding comparison -> required checkpoint -> durable claim/intent -> tool. Both callback and resumed approval use that path. A recovery retry gets a new grant and fresh authorization. Run the command until pass.

## Task 4: Separate core LLM Reviewer

**Files:** Modify `src/runtime/safety.py`, `src/model/llm.py`; create `tests/test_safety_reviewer.py`.

**Interfaces:** `CoreLLMReviewer(model=None, base_url=None, api_key=None)` with `review(facts: dict) -> ReviewVerdict`; facts are controller-constructed, bounded, sanitized analysis. Existing `llm.complete` accepts timeout/max_retries already. Use controller-known reason codes; failures produce ASK plus `reviewer_timeout`, `reviewer_invalid_response`, or `reviewer_error`. No raw model error/body is disclosed.

- [x] Write `CoreReviewerTests.test_isolated_request`: mock `llm.complete`; assert separate messages, selected model/provider, `tools=None`, `stream=False`, `timeout=30`, `max_retries=0`; main history and raw tainted artifacts absent.
- [x] Write `test_strict_schema_and_failure`: empty/extra-key/wrong-case JSON, tool calls, unknown reason code, timeout/provider error never ALLOW; incomplete analysis routes human without a provider call.
- [x] Write `test_reviewer_only_for_hard_review_auto_mode`: ALLOW, DENY, legacy ASK, mandatory human and other modes make zero calls. Reviewer ALLOW still encounters the Task 3 recheck.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_safety_reviewer -v`, observe failures; implement Reviewer; rerun and confirm pass.

## Task 5: CLI, headless and same-turn pending handling

**Files:** Modify `src/main.py`, `src/agent/loop.py`, `tests/test_main.py`, `tests/test_recovery.py`; update `docs/run_guide.md` and `docs/sandbox_v1_operations.md`.

**Interfaces:** CLI `--approval-mode {ask_all,ask_on_escalation,auto_review}`, `--host-policy PATH`, `--headless`, and `--prompt TEXT` for a noninteractive turn. `Agent.__init__` receives effective safety config/context/Reviewer and passes them to executor. `handle_pending_runtime_actions` drains fresh actions after run/resume; its existing input/print injection remains. Headless startup requires explicit session selection for resume; never invokes a chat chooser or input prompt. Reason codes go in ToolResult metadata and safe journal facts.

- [x] Write `ApprovalCliTests.test_defaults_and_host_constraints`: parser/Agent/executor propagation independent of PlanMode/WorkspaceMode; forbidden mode rejected; retained repo restrictions cannot be removed by a model edit.
- [x] Write `test_post_turn_approval_drains_batch`: two sequential approvals/rejections resume the interrupted batch; every tool message appears once; no new model call while approval unresolved.
- [x] Write `test_headless_never_reads_input`: ask_all ALLOW, REVIEW needing human, Reviewer timeout/ASK, old pending approval, recovery-required and chat selection all terminate with specified reason and no stdin calls.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_main tests.test_recovery -v`, observe new failures; implement and run again.
- [x] Run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q`; require no failures and record skipped Docker tests separately. Document S1 limitations and the S2 prerequisite rather than calling the current backend safe.

## Completion and handoff

Mark checkboxes only from fresh test evidence. Run `git diff --check` and review every changed authorization/replay branch. Keep unrelated dirty docs intact. Commits, if made, stage only explicit task files. Continue with `2026-10-04-safety-s2-private-files.md` after S1 passes; no automatic merge/deploy or claims of complete safety.

## Implementation record (2026-10-04)

Supported scope is implemented in the separate code-safety worktree.
Checklist coverage includes equivalent or combined tests where final test names
differ from the proposed names. See `../safety-acceptance.md` for current
evidence, review fixes, unsupported capabilities and deferred declassification.
Final discovery: 329 tests OK (11 opt-in physical skips). Separate rootless
physical run: 11 tests OK, zero skips. Independent review could not run
because the reviewer provider had no active credentials; author review
and regression verification are recorded explicitly. No commit or merge.
