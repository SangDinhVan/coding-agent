# Run the safe coding agent

The supported safety profile requires Linux rootless Docker and cgroups v2.
It admits an independent SHADOW copy of the selected source. Tool containers never
mount the original project. After a permitted write/edit, the controller publishes
that exact file to the selected project. Writable shell input, external effects
and network/package-fetch brokers are unavailable.

## Trusted setup

Team members need Python >=3.10, Docker Compose, and an installed/running Linux
rootless Docker daemon with cgroups v2. From a trusted checkout, run once:

```sh
python3 scripts/setup.py
```

Setup finds a local rootless context, checks cgroups v2, builds the sandbox from
the trusted sandbox-image/ directory, and writes its immutable image ID and
actual socket path into .env. It creates .env from .env.example if needed and
preserves existing provider settings. If it selects a different context, it
makes that context Docker's default so subsequent Compose commands use the same
daemon. It does not install or reconfigure the Docker daemon. Unsupported or
unavailable daemons and failed builds leave .env untouched.

Set API_KEY, MODEL and BASE_URL in your own .env. Do not share .env with teammates;
each runs setup on their own machine. No shell exports or copied image IDs are
needed. Run setup again after trusted sandbox helpers change. Build requires
access to pinned base images and locked packages on the first run; execution
containers still have network disabled.

Compose's control container has provider credentials and a daemon socket;
children receive neither. Runtime startup does not build or pull sandbox images,
and the controller verifies that the image's helpers match its trusted copies.
Build/setup only from a trusted controller checkout. The source selected with
--workspace is separate from the trusted controller; keep --state-root outside
that source. Selected ordinary source is authorized for the model provider and
local UI; private patterns and additive ignore exclusions remain in force.

## Modes and CLI

For Compose, save SANDBOX_IMAGE and optionally DOCKER_SOCKET in the controller
.env once. Compose reads them automatically; no shell exports are needed.
Use the rootless Docker context selected on the host. Select an approval mode
when starting a new session and rebuild the controller to pick up source changes:

```sh
docker compose run --build --rm coding-agent --approval-mode ask_all
docker compose run --build --rm coding-agent --approval-mode ask_on_escalation
docker compose run --build --rm coding-agent --approval-mode auto_review
```

Exit the current session before testing another mode. Start a new session rather
than resuming a legacy live session. The default is ask_on_escalation.

```sh
DOCKER_CONTEXT=rootless coding-agent --workspace /absolute/project \
  --state-root /absolute/private-state --image sha256:<trusted-id> \
  --approval-mode ask_on_escalation
```

- ask_all requests human approval for each new supported execution.
- ask_on_escalation permits ordinary bounded file operations and recognized static read-only commands; REVIEW goes to a human.
- auto_review uses the selected core model through an isolated request for recognized test commands with sealed project inputs. Commands that the normalizer cannot analyze still require human content review.
- Protected project controls always need exact-content human approval. No mode grants an unsupported physical capability.
- update_plan changes internal plan state. In ask_all it still asks for approval,
  but its preview contains only the plan request; it never reads workspace files
  or requests a filesystem grant.

To test ask_all in this repository, use a normal request, for example:

```text
Create an HTML website selling milk tea.
```

Choose 1 (allow) or 2 (deny) to decide immediately. Choose 3 (Note) to add an
optional note, then return to the menu and choose 1 or 2. Successful writes
appear immediately in the selected project; no copy/export step is needed.
The approval preview and tool result show the saved destination. Bash input is read-only, so it cannot create
HTML or modify project files. Workspace paths are supplied to the model without
reading file contents. The normalizer recognizes simple `pwd`, `ls`, `find`,
`grep`, `cat`, `head`, and `tail` commands with supported read-only options and
workspace-relative inputs. These receive bounded facts. Content-reading commands
screen their selected input files before execution; metadata-only commands do
not read file contents. They do not scan unrelated files. Shell expansion,
pipelines, redirection, unknown executables and
effectful options still require review. Known test commands also require review:
executing project code is not a static file check. Test and opaque shell input
must pass a trusted disclosure preflight before approval or execution.
If the full input contains possible secrets (including this
repository's test canaries), exceeds the preview budget, or contains binary files,
only that shell action is cancelled; the model can continue with read/write/edit.
No raw preview reaches the model, UI, Reviewer, or journal. Use the clean demo in
the manual test guide for shell approval tests. An actual secret-bearing read or
output still blocks the session: exit and start a fresh session; do not resume it
to clear its flags.

Simple tasks use file tools directly without a required plan. When a plan is
useful, successful `read`, `write`, and `edit` execution IDs can complete
evidence-required steps; reading a file can verify its contents without shell,
but does not prove browser behavior. The state frame lists regular tools,
successful evidence, and retry constraints. Two failures with the same reason
and intent forbid further retries until meaningful progress; the model must
choose an alternate tool. Repeatedly ignoring that guard fails the turn early.

--host-policy accepts a trusted JSON file outside source. Supported keys are
allowed_modes, default_mode, pinned_mode, profile_id, excluded_paths, human_gate_paths.
Repo .agent-safety.json accepts only excluded_paths and human_gate_paths.
Restrictions accumulate through journal replay; editing repo config cannot
remove an accepted restriction. Headless never prompts:

```sh
DOCKER_CONTEXT=rootless coding-agent --workspace /absolute/project \
  --state-root /absolute/private-state --image sha256:<trusted-id> \
  --headless --prompt 'Read main.py and describe it'
```

Human-required actions in headless return human_approval_unavailable without
running effects. Interactive approval/resume drains the pending batch before
another model call. Approvals bind arguments, generation, image, environment,
policy, security revision and effective mode, and are claimed once durably.

## Read-only tests

Each bash action receives a distinct sealed input copy mounted entirely RO.
It has network none, non-root user, rootfs RO, no capabilities, no-new-privileges,
cgroup CPU/memory/PID bounds, timeout/output bounds and /tmp byte/inode quotas.
The controller destroys the whole action container and verifies absence before
returning output. Namespace loopback remains possible. A cache-writing test
fails on RO project input; this never silently enables RW. Python uses
PYTHONDONTWRITEBYTECODE=1. Human sealed-input preview is bounded to 2 MiB of
UTF-8 data; unsupported or oversized previews block approval presentation.

## Review and export

Configure an existing directory outside source and state, and explicitly grant
the separate patch disclosure sink:

```sh
coding-agent --workspace /absolute/project --state-root /absolute/private-state \
  --image sha256:<trusted-id> --export-root /absolute/patches --allow-patch-export
```

/changes stops the file adapter, verifies quiescence, seals the final generation
and previews exact changes. /export review.patch prompts for the exact change
set digest. Its broker follows no symlink parents or leaves and never overwrites
an existing destination. Export root alone does not authorize disclosure.
Text create/delete/modify, newline state and file modes are represented; binary,
non-UTF-8, unsafe path names and empty-directory-only changes fail explicitly.
New CLI sessions already publish permitted write/edit actions; /apply is unnecessary.
Legacy sessions retain their private-only scope and still require patch export.
An exported patch is not a certificate that project code is safe to run on the host.

## Files in your project

The selected --workspace is the publication destination. In the default Compose
setup, /workspace is a bind mount of the current project folder. Starting a new
session enables publication; legacy session metadata keeps its original scope.
The controller copies only the exact authorized UTF-8 file, preserves its mode,
checks the project root and parent identities, refuses links/protected paths,
and rejects a changed before-image. A lock coordinates controller writers; external
editors must not save the same file concurrently during the atomic write. This is
per-file publication, not a transaction spanning an entire task. A failed/unknown
publication quarantines the session for manual reconciliation; no success is reported.
Deleting the sandbox does not undo files already saved in your project.

## Unknown outcomes and disclosure

Traces are saved to `.llm-traces/<session>/<turn>.txt` in the selected project,
outside the private sandbox, both with Compose and when running directly.
The CLI prints the saved path after a completed turn. Each file includes model
calls and `[runtime]` records with the tool, command or file path, working
directory, hard policy decision (`allow`, `review`, `ask`, `deny`), reason,
approval route, and Reviewer verdict when applicable. `phase: pre_effect`
identifies the policy recheck immediately before execution. Traces are excluded
from workspace snapshots and Docker builds, and pass the disclosure gate.
Model requests use a finite 120-second timeout and up to two SDK retries for
transient connection/timeout/rate-limit/server failures before a response starts.
An interrupted response stream is not replayed automatically. Persistent timeout
and connection failures are reported as model_timeout and model_connection_failed.
Security flags and provenance survive journal replay, compaction and project
memory. Main model, Reviewer, summary, memory, UI, audit text, trace and patch
export all have pre-sink gates. Known secret exposure blocks them and further
OS actions. Pattern screening cannot detect every encoded or unknown secret.
Declassification is unavailable: flags remain monotonic.

Unknown transport, cleanup or post-effect audit outcomes require manual
reconciliation. Do not retry or export until reconciled. Safe startup refuses
legacy host apply journals rather than restoring source automatically. /discard
removes the sandbox, retaining published project files. Recovery evidence must match
both the private file and published source state; conflicting source changes/backups
are preserved by the internal legacy recovery API, which safe CLI does not expose.

See [acceptance evidence](superpowers/safety-acceptance.md) and
[operations](sandbox_v1_operations.md).
