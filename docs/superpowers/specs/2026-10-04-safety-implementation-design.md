# Safety implementation design

Date: 2026-10-04.
Status: User approved on 2026-10-04; four implementation plans are ready for
review and execution-method selection. Product implementation has not started.
Requirements: [safety.md](../../safety.md).
Previous investigation: [safety redesign](2026-10-03-safety-redesign.md).

## 1. Intent and success criteria

Implement the approved safety architecture in the existing Python coding agent.
Main LLM proposes actions, hard policy classifies them, the controller obtains
the required review, and enforced adapters determine the actual physical scope.
Neither model output nor repository content creates authority.

Keep the existing event journal, reducer, tool execution IDs, approval/resume
flow, Docker backend, and descriptor-relative filesystem helper. Do not create
a second state machine, memory store, policy service, or policy DSL. No new
product dependencies; Python >=3.10 and the installed OpenAI SDK suffice.

A release is accepted only when a normal private read/edit/test workflow works,
the mode matrix passes, and physical Docker tests demonstrate its advertised
profile. Control-plane unit tests alone do not establish isolation. Each
milestone states which capabilities remain unavailable.

## 2. Concrete choices proposed for review

These choices resolve safety.md section 8; they are proposals, not claims that
the user has already approved these implementation details.

| Choice | Initial implementation |
| --- | --- |
| Workspace | SHADOW by default. Safe profile rejects LIVE, including resumed LIVE metadata. No silent conversion of an existing session. |
| Publication | Sealed patch export; no automatic host apply. Existing apply functionality is not exposed by the safe CLI. |
| Approval | `ask_on_escalation` default; support `ask_all` and `auto_review` within trusted host constraints. |
| Interaction | Interactive REPL by default; explicit `--headless` never prompts or creates new WAITING_APPROVAL state. |
| Source disclosure | User selects the admitted source tree and authorizes its ordinary contents for the configured model provider and local approval UI. Hard private exclusions remain; `.agentignore` only narrows admission. Approval metadata stores a selection digest. |
| Execution content | Bind the complete sealed generation for opaque execution. Use trusted structural analysis for Reviewer facts; incomplete analysis requires human review of sealed content. Missing enforcement always denies. |
| Storage | Start with bounded file adapters and read-only execution inputs, bounded temporary storage, and bounded harvest. Writable opaque execution requires separately verified hard byte/inode quotas; the existing watchdog is only an observation mechanism. |
| Trusted image | Use a locally present pinned image digest. Do not build the trusted image from the agent-editable project during startup. |
| External effects | No network, package-fetch, external MCP, email, push, or API mutation broker in this implementation. Hard DENY. |

Read-only execution is useful for inspection but does not claim to support every
project test runner. A test requiring project writes is denied until the RW
profile passes its enforcement acceptance tests.

## 3. Approaches and selection

1. Recommended: strengthen the current controller and backend in dependency
   order, with explicit capabilities and DENY for an unsupported profile. This
   preserves the existing runtime and allows each milestone to be tested.
2. Replace the runtime with a new authorization framework. This duplicates the
   durable execution/approval lifecycle and adds migration risk without an
   additional enforcement boundary; reject it.
3. Add approval modes alone and keep the current shared RW container. This
   changes user experience but cannot satisfy the source, protected-path,
   process-lifetime, or disclosure contracts; reject it as the final design.

## 4. Control-plane contracts

Place safety records and policy/router logic in `src/runtime/safety.py`; add
execution/replay fields to `src/runtime/models.py` and `src/runtime/reducer.py`.
Keep the controller in `src/runtime/executor.py`. Split policy into another
module only if its implemented size warrants it.

### 4.1 Configuration and authority

`ApprovalMode`: `ask_all`, `ask_on_escalation`, `auto_review`.
`InteractionMode`: `interactive`, `headless`.
`PolicyDecision`: retain `ALLOW`, `ASK`, `DENY`; add `REVIEW`.
Legacy `ASK` always means mandatory human approval, never automatic review.

`SafetyConfig` is a frozen controller-owned record containing effective modes,
host revision, allowed modes, pinned mode if any, profile ID, and effective
constraints digest. CLI options are independent of plan/workspace mode.
Disallowed mode returns `approval_mode_not_permitted`; malformed configuration
returns `invalid_approval_configuration`. Neither falls back to another mode.

`AuthorityRecord` binds session ID, admitted source digest, provider/UI/export
sink permissions, allowed operations and private workspace scope. It is created
only through the trusted startup/control-plane path and persisted in private
session state. Repo-local settings cannot supply or replace it.

Repo restrictions merge monotonically with host/user constraints. Removing or
editing repository config cannot relax restrictions already accepted in this
session. A relaxation needs a separate user action within host limits.

### 4.2 Normalization and hard policy

`NormalizedAction` records tool kind, canonical relative targets/cwd, exact
argument digest, generation digest, and opaque execution/egress/deletion flags.
Digest canonical JSON with SHA-256; reject non-object arguments, unknown keys,
incorrect types, unsupported enum values, invalid paths, and oversized inputs.
Do not execute shell AST fragments or infer safety from an executable name.

`PolicyResult` contains decision, stable reason code, mandatory-human flag,
supported profile/scope facts, and immutable action binding.

Missing policy, policy exceptions, missing authority/generation/enforcement
facts, unsupported tool/profile, or forbidden scope produces DENY before review.
Ordinary scoped file operations may be ALLOW. Opaque execution is REVIEW only
if its complete physical scope is supported. Protected project control writes
require exact-diff human approval through the file adapter. Effective trusted
runtime/state/settings and secrets remain DENY.

### 4.3 Review routing

Apply DENY first, mandatory human gate second, approval mode third:

| Hard result | ask_all | ask_on_escalation | auto_review |
| --- | --- | --- | --- |
| ALLOW | Human | Revalidate | Revalidate; no Reviewer call |
| REVIEW | Human | Human | Reviewer ALLOW -> revalidate; ASK -> human; DENY -> block |

Headless human-required actions terminate with
`human_approval_unavailable`. Preserve the original review failure reason in
safe audit metadata. An unresolved recovery also stops headless execution with
`recovery_required`; it is not a request to retry.

`CoreLLMReviewer` uses `model.llm.complete` with the same effective
model/provider, a separate system/messages array, `tools=None`, `stream=False`,
timeout 30 seconds, and no retries. Controller validates exactly the decision
schema `{decision: allow|ask|deny, reason_code: string}` and rejects extra keys,
tool calls, empty/malformed responses, or invalid decisions. Restrict reason
codes to controller-recognized values; do not log arbitrary model rationale.

Reviewer input consists of authenticated goal, bounded normalized facts,
effective scope, security state, and trusted artifact analysis. No complete main
conversation or raw tainted code/stdout is sent. Incomplete required analysis
routes to human without asking the Reviewer to infer missing facts.
Timeout/provider/schema errors route to human in interactive mode, DENY in
headless. Reviewer ALLOW cannot grant scope or bypass revalidation.

### 4.4 Binding, revalidation, and one-use execution

Bind execution/session/tool/argument identities, sealed input generation,
image and environment digests, cwd, profile and scope, authority ID, policy
revision, security version, modes, and constraints digest. Human approval and
Reviewer verdict refer to this binding, not just a boolean or execution name.

Persist a safe authorization event with the binding before waiting for human
approval. The approval UI renders controller facts and exact sealed changes or
execution scope. It does not treat model rationale as verified facts.

Both callback and pending/resume approvals re-run hard policy and compare the
binding immediately before effects. A change invalidates old approval; perform
new evaluation/review, and block if the new policy denies. A matching approval
for the same pending execution satisfies the human gate once, including
`ask_all`. It does not authorize the next execution or whole batch.

Require immutable before-images and durable execution intent before effects.
An exclusive controller journal writer durably claims the grant once with
max_uses=1. A crash after claim is unknown/recovery-required, never automatic
replay. Completed/denied execution IDs cannot execute again. A deliberately
authorized recovery retry must use a new grant and fresh binding.

REPL drains pending approvals/recovery after run/resume as well as startup,
then resumes the interrupted batch before model progress. Record every tool
result exactly once. Existing journal approvals without a complete binding
must be reviewed again; old journal read/recovery remains supported.

## 5. Workspace and enforced execution

### 5.1 Admission and immutable inputs

Reject source/state-root overlap before creating control directories. Use
descriptor-relative no-follow admission; selected regular files must have one
hardlink. Reject symlinks and special files rather than copying them. Check
source inode/type/size/mtime before and after copy, with bounded entries/bytes;
unverified concurrent source changes abort admission.

Copy selected dirty/untracked bytes into independent private inodes. Keep
before-generation outside every untrusted mount, immutable to sandbox writers.
Working copies and checkpoints preserve hashes, types, modes and absent
targets. The mutable workspace or a Git commit alone is not a generation.

### 5.2 Filesystem adapters

Extend the existing `sandbox_fs.py` no-follow descriptor traversal. Host policy
constructs the allowed operation/targets/content and budget; the model does not
construct the adapter grant. Use atomic bounded replacement, validate existing
and absent targets, checkpoint before writes, and prevent writes to excluded
or controller targets. A control-file write uses a one-use exact-content grant.

### 5.3 Opaque execution

Use a disposable container/cgroup for each execution. Do not share shell process
lifetime with later actions. Pin helper/image, mount only the approved private
scope, scrub env/FDs/credentials, network none, rootfs RO, drop all capabilities,
no-new-privileges, and enforce CPU/memory/PID/time/output budgets.

The initial opaque profile mounts the entire input generation RO; bounded /tmp
is its writable scope and output files there are not promoted automatically.
Its grant states this complete scope. A future RW profile must enforce protected
paths and ancestor rename/unlink/recreate, including absent targets, plus hard
byte/inode quotas. Mounting only existing protected files RO is insufficient.
If those guarantees cannot be verified, DENY RW shell even after human approval.

Terminate the whole action container on success/error/timeout/cancel, including
detached/setsid children. Verify quiescence before bounded harvest or the next
action. Transport loss or uncertain termination quarantines effects and stops
mutation/publication. Network none permits namespace loopback; do not advertise
absolute localhost denial.

## 6. Security state, disclosure, export, and recovery

Replay controller-owned security state from the existing journal:
`untrusted_content_seen`, `agent_modified_content`, `injection_flags`,
`secret_exposure_detected`, `provenance_generation_ids`, `security_state_version`.
Update it before any output reaches a sink and before the next batch action.
Flags narrow scope; clearing them requires an authenticated control-plane action.

Disclosure checks precede Main LLM, Reviewer, summarizer, memory, UI, textual
audit/trace, and patch export. Carry provenance through existing memory/context
paths without changing retrieval or storage architecture. Known secret exposure
blocks disclosure; screening is supplementary and does not promise detection
of unknown/encoded secrets. Keep raw outputs bounded and quarantined until gate
evaluation. Trace stays in private state and is not copied into the project.

Harvest only quiescent data, rejecting links, special files and unsafe modes;
seal immutable artifacts with bounded entries/bytes/time. Record separate
process outcome, enforcement observation, and effect state. Exit zero is not
evidence of safety, and missing observations are UNKNOWN.

Export a digest-bound patch to a pre-authorized safe sink using no-follow/beneath
handling. Never invoke hooks/filters/builds to generate or inspect a patch.
No automatic project writes. Host publication/writer coordination and guarded
multi-file recovery belong to a later separately approved design. Private
recovery restores only when expected-after evidence and writer exclusion hold;
otherwise keep backups and require reconciliation. No automatic retry after
post-effects audit/transport failure.

## 7. Rollout and plan boundaries

Each row receives a task-by-task implementation plan after written-spec review.
Order is a dependency chain, not parallel independent implementation.

| Milestone | Deliverable and existing code boundaries | Release gate |
| --- | --- | --- |
| S1: Control-plane decisions | `runtime/safety.py`, `runtime/models.py`, `runtime/executor.py`, `runtime/reducer.py`, `agent/loop.py`, `main.py`; approval matrix, fail-closed policy, binding/rechecks, headless/REPL, Reviewer with mocked transport | Unit regression suite; backend capabilities remain explicit and unsupported OS actions DENY. This is not the complete safe profile. |
| S2: Private inputs and file effects | `core/paths.py`, `sandbox/workspace.py`, `sandbox/session.py`, `sandbox/models.py`, `sandbox_fs.py`; admission, generations, scope grants, checkpoints, exact control diff | Inode/race/link/budget tests plus physical Docker file-adapter tests; ordinary read/edit supported. |
| S3: Disposable RO execution | `sandbox/docker.py`, `sandbox/session.py`, `sandbox_exec.py`, trusted image/startup files; action lifetime, pinned image, RO generation, bounded resources | Physical descendant, mounts/env, network, quota and transport/quarantine tests; RO inspection/test workflows supported with documented write limitations. |
| S4: Disclosure and sealed export | `memory/event_store.py`, `memory/manager.py`, `context/checkpoint.py`, `context/compactor.py`, `model/llm.py`, `agent/loop.py`, `sandbox/changes.py`; shared state/provenance, pre-sink gate, safe trace and export | Canary/injection/compact/resume/batch/export tests; usable safe CLI accepted only after S1-S4 gates pass. |

Writable arbitrary execution, automatic host publication, standing approvals,
SLM migration and external brokers remain separate projects. Their absence must
be shown as unavailable capability, never hidden by approval or mock evidence.

## 8. Acceptance and verification

Use stdlib `unittest` and existing fakes; no new test framework. Local regression
command: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`.
Run Docker acceptance tests on the supported rootless host with the pinned
trusted image and record actual skips separately from passes.

Required cases, mapped to safety.md sections 5 and 7:

- S1: All mode/hard-result/headless combinations; hard DENY invokes neither
  Reviewer nor human; legacy ASK never invokes Reviewer; mandatory gates cannot
  be auto-approved; missing/raising policy fails closed.
- S1: Reviewer invalid/timeout/ASK/DENY; hard ALLOW makes zero review calls;
  callback and resumed approval both recheck; content/mode/policy/taint changes
  invalidate binding; one ID has effects once; legacy unbound approval re-gates.
- S1: Immediate REPL approval/rejection, multiple pending actions and same-turn
  batch resume produce exactly one result per tool without premature model calls;
  headless pending approval/recovery does not read stdin.
- S2: State-root overlap, internal/external symlink, multiply linked regular
  files, source replacement/change during copy, absent target and protected diff;
  original dirty/untracked bytes and inodes remain untouched.
- S3: Success with child closing stdio/setsid, timeout/cancel, failed stop and
  malformed transport; no live descendants before harvest; forbidden RW
  profile denies before execute; verify real network/mount/resource evidence.
- S4: Known secret in read/stdout/diff/summary/trace is gated before provider/UI/
  log/export; provenance survives compact/resume; security updates affect the
  next action of the same batch; symlink export sink cannot redirect writes.
- S4: Sealed patch matches admitted before-state and reviewed after-state;
  export never applies to source; recovery refuses conflicting changes and
  preserves backup; post-effect audit loss never causes automatic reexecution.
- End to end: Edit an ordinary source file, inspect/test in the advertised RO
  profile, export the sealed patch, and confirm original source remains intact.

Do not relabel a skipped Docker acceptance run as completed enforcement work.
The implementation report must state the current milestone, test evidence,
unsupported capabilities, and remaining safety.md acceptance gaps.
