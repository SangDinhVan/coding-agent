# Sandbox V1 Operations

## Status and boundary

Sandbox V1 is the physical-enforcement layer for this coding agent. The LLM,
event journal, review UI, and lifecycle controller run in the trusted control
plane. Model-controlled `bash`, `read`, `write`, and `edit` run only in one
rootless Docker child container.

New sessions use a **live workspace**: the host repository is bind-mounted RW at
`/workspace`, so successful tool writes are visible immediately in the host IDE
and `git diff`. Existing sensitive paths, explicit `.agentignore` selections,
and known cache/build directories are over-mounted with protected empty
read-only file/directory masks. Ordinary `.gitignore` entries remain writable;
Git versioning policy is not an access-control boundary. The child receives
neither the rootless Docker socket nor host paths outside the workspace.

> **Important:** live changes are already applied. `/apply` is unnecessary and
> `/discard` only closes the sandbox; it does not restore repository files.
> Review with the IDE or `git diff`, and undo with normal Git/IDE operations.

## Requirements

- Linux with rootless Docker Engine, or Docker Desktop on Windows/macOS using
  Linux containers
- cgroups v2 in the daemon VM/host
- Local immutable image reference (`sha256:<64-hex>` image ID or
  `repository@sha256:<64-hex>` registry digest)
- At least 5 GiB free by default in the state filesystem

Verify the daemon:

```bash
docker context show
docker info --format 'os={{.OperatingSystem}} version={{.ServerVersion}} security={{json .SecurityOptions}} cgroup={{.CgroupVersion}}'
```

Preflight classifies the daemon as `ROOTLESS`, `VM_ISOLATED`, or
`ROOTFUL_BARE`. `ROOTLESS` and Docker Desktop `VM_ISOLATED` are accepted;
`ROOTFUL_BARE` fails closed. Docker Desktop's Linux VM boundary is acceptable
for this project, but is **not equivalent to a genuinely rootless daemon**.

## Run with one Docker command

For the normal team workflow, run from the repository root:

```bash
docker compose run --build --rm coding-agent
```

The Compose launcher builds the trusted control-plane image, builds the sandbox
image from the restricted `sandbox-image/` context, obtains its immutable local
`sha256:<64-hex>` image ID, and passes that exact ID to the agent. It mounts the
host daemon socket only into the trusted control plane; sandbox children never
receive the socket. The control plane inspects its own Compose mounts and
translates `/workspace` and `/state` to daemon-visible sources before creating a
child, so host path syntax does not leak into Linux-container code.

Resume the latest session with:

```bash
docker compose run --build --rm coding-agent resume --last
```

A session created by an older version remains legacy shadow mode after resume;
it is never migrated or copied into the repository automatically.

Docker Desktop on Windows/macOS uses the default `/var/run/docker.sock` source
inside its Linux VM. On Linux, Compose uses `$XDG_RUNTIME_DIR/docker.sock` when
`XDG_RUNTIME_DIR` is set. To select a different socket explicitly:

```bash
DOCKER_SOCKET="$XDG_RUNTIME_DIR/docker.sock" docker compose run --build --rm coding-agent
```

Do not mount Windows `\\.\pipe\docker_engine` into the Linux control-plane
container; the Linux Docker CLI communicates through the VM's Unix socket.

## Build and pin the sandbox image manually

Build only from the restricted image context:

```bash
docker build -t sang-coding-agent-sandbox:local ./sandbox-image
docker image inspect sang-coding-agent-sandbox:local \
  --format '{{json .RepoDigests}} {{.Id}} {{.Config.User}}'
```

Current verified image:

```text
sang-coding-agent-sandbox@sha256:4e029dc9ecd6501358ac4edcd69985ec4eb32dbcba732d8efcc6ed87183b9302
```

Base image:

```text
python@sha256:d893452fcd120ea9a7233972c85ea868255bde289a636fe76ff090427fe8fac9
```

Never replace either digest with `latest` or another mutable tag.

## Direct host start (advanced)

```bash
coding-agent \
  --workspace "$PWD" \
  --image 'sang-coding-agent-sandbox@sha256:4e029dc9ecd6501358ac4edcd69985ec4eb32dbcba732d8efcc6ed87183b9302'
```

Resume the most recent stopped session:

```bash
coding-agent \
  --workspace "$PWD" \
  --image 'sang-coding-agent-sandbox@sha256:4e029dc9ecd6501358ac4edcd69985ec4eb32dbcba732d8efcc6ed87183b9302' \
  resume --last
```

Resume reconciles the exact container ID, session/workspace/image/mode labels,
immutable image, bind source, runtime user, masks, network mode, read-only root
filesystem, capability drop, no-new-privileges, and CPU/memory/PID limits before
model or tool execution. Legacy metadata may omit only the mode label.

## Runtime security contract

- Daemon isolation must be `ROOTLESS` or Docker Desktop `VM_ISOLATED`;
  `ROOTFUL_BARE` fails closed. cgroups v2 remains mandatory.
- Immutable image digest and `--pull never` prevent mutable-image drift.
- `--network none`, read-only rootfs, all capabilities dropped, and
  `no-new-privileges` remain enabled.
- CPU, memory/swap, PID, bounded `/tmp`, output, timeout, and workspace-growth
  limits remain active.
- No Docker socket, host home, inherited credentials, or paths outside the
  selected workspace are mounted into the child.
- Existing `.git`, `.env*`, credential paths, known cache/build directories,
  and paths selected by `.agentignore` are hidden with empty read-only bind
  mounts from protected state. Ordinary `.gitignore` entries remain accessible
  and writable in live mode.
- Mask sets are persisted per session. Start a new session after changing mask
  policy or ignore configuration; resume intentionally reconciles the original
  session contract.
- A runtime inspect/probe verifies the exact contract after start and resume;
  mismatch stops the container and fails closed.

### Why live uses child user `0:0`

The sandbox image still declares non-root user `65532:65532`, which legacy
shadow sessions use. Host repositories are normally owned by the invoking host
user and cannot be written by that identity. A live child therefore runs as
`0:0` inside the daemon boundary. With rootless Docker this maps to the invoking
unprivileged host user; with Docker Desktop it is root only inside the Linux VM,
not the Windows/macOS host. Capability drop, no-new-privileges, read-only
rootfs, and network isolation still apply. Docker Desktop `VM_ISOLATED` is a
weaker assurance than true rootless operation and is accepted only for this
project's scope.

## Resource options

Defaults and hard ceilings:

| CLI option | Default | Hard ceiling |
|---|---:|---:|
| `--cpus` | 2 | 4 |
| `--memory-mib` | 2048 | 4096 |
| `--pids` | 256 | 512 |
| `--tmpfs-mib` | 512 | 1024 |
| `--tool-timeout` | 60 s | 600 s |
| `--workspace-growth-mib` | 4096 | 8192 |

The workspace growth limit is a host-watchdog soft quota. It can overshoot by
one measurement interval. CPU, memory, and PID limits are cgroup-enforced.

## User control commands

### New live sessions

- `/changes`: points to `git diff` or IDE review; it does not seal or stop the
  live session.
- `/apply`: reports that changes are already present; no apply operation runs.
- `/discard`: destroys the child sandbox but leaves live repository changes
  untouched.
- `exit`: stops the child and retains session state for resume/inspection for
  24 hours.

A typical review/undo flow is:

```bash
git status --short
git diff
# Then use the IDE or a targeted Git restore command after reviewing the paths.
```

Do not run a broad restore/reset blindly when the working tree already contains
user changes.

### Legacy shadow sessions

Metadata without `workspace_mode` is treated as `shadow`. These sessions retain
the original contract:

- `/changes`: stop the container, safely scan output, and seal one deterministic
  whole-set hash.
- `/apply`: require `approve <exact-hash>` plus a non-empty note, revalidate the
  seal, check conflicts, then apply with a rollback journal.
- `/discard`: destroy the container and reject its shadow-only changes.

One violation or host conflict blocks the entire legacy set. There is no
safe-subset apply. Never manually copy an unapproved legacy shadow workspace or
a partial apply journal into the host repository.

All four commands are intercepted by the trusted CLI and are not model tools.

## State layout

Default root:

```text
${XDG_STATE_HOME:-~/.local/state}/sang-coding-agent/
```

Important entries:

```text
chats/<session-id>.jsonl
sandboxes/<session-id>/metadata.json
sandboxes/<session-id>/heartbeat
sandboxes/<session-id>/baseline-manifest.json
sandboxes/<session-id>/mask-file
sandboxes/<session-id>/mask-directory/
sandboxes/<session-id>/final-manifest.json        # legacy shadow
sandboxes/<session-id>/changeset.json             # legacy shadow
sandboxes/<session-id>/shadow.git/                 # legacy shadow
sandboxes/<session-id>/rollback/                   # legacy shadow
sandboxes/<session-id>/workspace/                  # legacy shadow
projects/<workspace-identity>/PROJECT.md
```

The state root and session directories are host-private. Live mask sources are
stored there and mounted read-only over matching repository entries. A live
session's main mount source is the host workspace; a shadow session's main mount
source remains its state workspace.

## Verification

Unit tests (Docker integration is skipped unless opted in):

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

Real rootless security integration:

```bash
RUN_SANDBOX_INTEGRATION=1 \
SANDBOX_IMAGE='sha256:4e029dc9ecd6501358ac4edcd69985ec4eb32dbcba732d8efcc6ed87183b9302' \
./.venv/bin/python -m unittest tests.test_sandbox_integration -v
```

## Crash recovery and cleanup

- Ambiguous Docker/helper transport stops the entire child and verifies the
  stopped state.
- Tool timeout stops the whole child so detached descendants cannot survive.
- Live repository changes survive stop, crash, `/discard`, and TTL cleanup.
  Review or undo them with Git/IDE.
- An incomplete legacy host-apply journal blocks new work until rollback
  recovery completes or explicit manual recovery is required.
- Metadata and legacy shadow/rollback evidence are retained after normal exit.
  The janitor removes only expired sessions whose metadata has the exact managed
  marker and matching full session ID.
- Never delete containers by fuzzy name/prefix. Inspect exact labels and session
  metadata first.

Incident check:

```bash
docker ps -a \
  --filter 'label=io.sang-coding-agent.managed=true' \
  --format '{{.ID}} {{.Names}} {{.Status}} {{.Label "io.sang-coding-agent.session"}}'
```

If stop/reconciliation or legacy apply recovery fails, preserve the session
directory and container metadata; do not resume tools or manually copy a partial
change set.

## Known V1 limitations

- On rootless Linux, Docker shares the host kernel; this is not VM-strength
  isolation. On Docker Desktop, the daemon and children run in a Linux VM, but
  `VM_ISOLATED` is still not equivalent to rootless Docker.
- Live arbitrary shell can modify or delete any unmasked path in the repository.
  There is no whole-set approval or automatic live rollback.
- Mask discovery covers sensitive/ignored entries that exist when the session is
  created. A later command can create a new denylisted filename that had no mount
  at creation time; review new files with Git/IDE and do not store repository
  secrets as ordinary files.
- Docker's comma-delimited `--mount` syntax cannot safely represent a workspace
  or mask path containing a comma; session creation fails closed for such paths.
- Filename denylisting does not find secrets hidden inside ordinary source files.
- Disk quota is soft, not a filesystem project quota.
- Network is always disabled. Dynamic `pip`, `npm`, Git fetch/push, and other
  downloads are unavailable; dependencies must be baked into the image.
- Per-action checkpoints and kernel-enforced per-path capabilities are future
  work.
- `sandbox_violation` is tri-state evidence; arbitrary shell syscall attempts
  cannot always be distinguished from ordinary runtime failure.
