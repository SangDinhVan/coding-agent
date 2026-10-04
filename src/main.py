import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from agent.loop import Agent
from core.config import MODEL
from core.paths import ControlPaths
from memory.event_store import DEFAULT_CHATS_DIR, create_chat_path, list_chats, resolve_chat
from runtime.models import RecoveryDecision, ReplayPolicy
from runtime.safety import ApprovalMode, AuthorityRecord, DisclosureDenied, InteractionMode, SafetyConfig, digest_json, load_safety_config
from sandbox.changes import recover_incomplete_apply
from sandbox.docker import DockerBackend
from sandbox.models import ResourceLimits, SandboxStatus, WorkspaceMode
from sandbox.session import SandboxSession
from tools.registry import ToolRegistry

YELLOW = "\033[93m"
RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent", description="Personal AI coding agent")
    parser.add_argument("--workspace", default=".", help="Host workspace to snapshot")
    parser.add_argument("--state-root", default=str(ControlPaths.default_root()), help="Host-only sandbox state root")
    parser.add_argument("--image", required=True, help="Immutable sandbox image: sha256 image ID or repository@sha256 digest")
    parser.add_argument("--control-container-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--pids", type=int, default=256)
    parser.add_argument("--tmpfs-mib", type=int, default=512)
    parser.add_argument("--tool-timeout", type=int, default=60)
    parser.add_argument("--workspace-growth-mib", type=int, default=4096)
    parser.add_argument('--approval-mode', choices=[mode.value for mode in ApprovalMode], default=None)
    parser.add_argument('--host-policy', type=Path)
    parser.add_argument('--export-root', type=Path, help='Existing separately authorized patch export directory outside source/state')
    parser.add_argument('--allow-patch-export', action='store_true', help='Explicitly authorize patch disclosure to --export-root; each export still needs its exact digest')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--prompt', help='One noninteractive turn; requires --headless')
    subparsers = parser.add_subparsers(dest="command")
    resume = subparsers.add_parser("resume", help="Resume a previous chat")
    resume.add_argument("session", nargs="?", help="Full session ID or unique prefix")
    resume.add_argument("--last", action="store_true", help="Resume the most recent chat")
    return parser


def select_chat_path(
    args: argparse.Namespace,
    chats_dir: str | Path = DEFAULT_CHATS_DIR,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> Path:
    if args.command != "resume":
        return create_chat_path(chats_dir)
    if args.last and args.session:
        raise ValueError("Không thể dùng session ID cùng với --last.")
    if args.last or args.session:
        return resolve_chat(chats_dir, args.session)
    if getattr(args, 'headless', False):
        raise ValueError('session_selection_required')
    chats = list_chats(chats_dir)
    if not chats:
        raise ValueError("Chưa có cuộc chat nào để resume.")
    print_fn("Các cuộc chat gần đây:")
    for index, chat in enumerate(chats, 1):
        updated = datetime.fromtimestamp(chat["updated_at"]).strftime("%Y-%m-%d %H:%M")
        print_fn(f"  {index}. [{chat['id'][:8]}] {chat['title']} ({updated})")
    choice = input_fn("Chọn số hoặc nhập ID: ").strip()
    if choice.isdigit():
        index = int(choice)
        if not 1 <= index <= len(chats):
            raise ValueError(f"Lựa chọn phải từ 1 đến {len(chats)}.")
        return chats[index - 1]["path"]
    return resolve_chat(chats_dir, choice)


def handle_pending_runtime_actions(agent, input_fn=input, print_fn=print) -> None:
    handled = set()
    def signature(action):
        execution = agent.runtime_state.executions[action.execution_id]
        return action.execution_id, getattr(execution, 'approval_binding_digest', None)
    while True:
        actions = [action for action in agent.pending_runtime_actions() if signature(action) not in handled]
        if not actions:
            return
        config = getattr(agent, 'safety_config', None)
        if isinstance(config, SafetyConfig) and config.interaction_mode == InteractionMode.HEADLESS:
            raise RuntimeError('recovery_required' if any(action.kind == 'recovery' for action in actions) else 'human_approval_unavailable')
        for action in actions:
            previous_signature = signature(action)
            _resolve_runtime_action(agent, action, input_fn, print_fn)
            handled.add(previous_signature)
        if any(signature(action) not in handled for action in agent.pending_runtime_actions()):
            continue
        if isinstance(agent.runtime_state.active_turn_id, str):
            agent.resume_active_turn()


def _resolve_runtime_action(agent, action, input_fn, print_fn):
        execution = agent.runtime_state.executions[action.execution_id]
        print_fn(f"[{action.kind}] {action.execution_id[:8]} {action.tool_name}: {action.message}")
        if action.kind == "approval":
            presenter = getattr(agent, 'approval_presentation', None)
            if callable(presenter):
                facts = presenter(action.execution_id)
                if isinstance(facts, dict):
                    print_fn(json.dumps(facts, ensure_ascii=True, indent=2))
            note = ""
            while True:
                print_fn("1: allow\n2: deny\n3: Note")
                choice = input_fn("Approval decision (1/2/3): ").strip()
                if choice in {"1", "2"}:
                    agent.resolve_approval(action.execution_id, choice == "1", note)
                    return
                if choice == "3":
                    note = input_fn("Approval note: ").strip()
                else:
                    print_fn("Choose 1, 2, or 3.")
        allowed = [RecoveryDecision.COMPLETED, RecoveryDecision.FAILED]
        if execution.replay_policy != ReplayPolicy.MANUAL:
            allowed.append(RecoveryDecision.RETRY)
        allowed_text = "/".join(item.value for item in allowed)
        while True:
            value = input_fn(f"Recovery decision ({allowed_text}): ").strip().lower()
            try:
                decision = RecoveryDecision(value)
            except ValueError:
                continue
            if decision in allowed:
                break
        note = input_fn("Recovery evidence/note: ").strip()
        agent.resolve_recovery(action.execution_id, decision, note)


def run_headless(agent, prompt):
    handle_pending_runtime_actions(agent)
    if agent.runtime_state.active_turn_id:
        result = agent.resume_active_turn()
    elif not prompt:
        raise ValueError('headless_prompt_required')
    else:
        result = agent.run_turn(prompt)
    if agent.turn_state and agent.turn_state.error:
        raise RuntimeError(agent.turn_state.error.category)
    return result


def _render_changes(changes) -> str:
    lines = [f"Change set: {changes.change_set_hash or '(blocked)'}"]
    for entry in changes.entries:
        lines.append(f"  {entry.operation:6} {entry.kind:7} {entry.path} ({entry.size} bytes)")
        if entry.preview:
            lines.append(entry.preview)
    lines.extend(f"  VIOLATION: {reason}" for reason in changes.violations)
    return "\n".join(lines)


def _default_agent_factory(
    events_path: Path,
    workspace: Path,
    state_root: Path,
    image: str,
    limits: ResourceLimits,
    control_container_id: str = "",
    safety_config: SafetyConfig | None = None,
    export_root: Path | None = None,
    allow_patch_export: bool = False,
):
    source = workspace.expanduser().resolve(strict=True)
    paths = ControlPaths.create(state_root, events_path.stem, source)
    backend = DockerBackend(image, control_container_id=control_container_id)
    if paths.metadata.exists():
        metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
        backend.container_id = metadata.get("container_id")
        backend.session_id = events_path.stem
    session = SandboxSession(
        events_path.stem, source, paths, backend, limits,
        mode=WorkspaceMode.SHADOW,
        excluded_paths=(safety_config.excluded_paths if safety_config else ()),
        publish_files=True,
    )
    if (paths.rollback / 'apply-journal.json').exists():
        raise RuntimeError('recovery_required: legacy host apply requires manual reconciliation')
    if paths.metadata.exists():
        if session.status == SandboxStatus.STOPPED:
            session.resume()
            session.start_watchdog()
        elif session.status != SandboxStatus.SEALED:
            raise RuntimeError(f"sandbox cannot restore from {session.status.value} state")
    else:
        session.create()
        session.start()
        session.start_watchdog()
    export_authority = None
    if allow_patch_export:
        if export_root is None or not export_root.is_dir() or export_root.is_symlink():
            raise ValueError('existing_export_root_required')
        export_authority = AuthorityRecord(digest_json({'sink': 'patch_export', 'root': str(export_root.absolute())}),
            events_path.stem, session.authority.source_selection_digest, str(export_root.absolute()), ('export_patch',), ('patch_export',))
    return Agent(
        model=MODEL,
        workdir=str(source),
        events_path=str(events_path),
        sandbox=session,
        tool_registry=ToolRegistry(session),
        state_root=state_root,
        workspace_identity=paths.workspace_identity,
        safety_config=safety_config,
        export_root=export_root,
        export_authority=export_authority,
    )


def run_repl(
    events_path: Path,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    agent_factory: Callable[..., object] = _default_agent_factory,
    factory_args: tuple = (),
) -> None:
    agent = agent_factory(events_path, *factory_args)
    raw_print = print_fn
    if callable(getattr(agent, 'gate_text', None)):
        def print_fn(text):
            try:
                raw_print(agent.gate_text(str(text), 'ui'))
            except DisclosureDenied as error:
                if error.reason_code == 'secret_exposure':
                    raw_print('disclosure_denied: possible secret content detected in the request or workspace preview. '
                              'This session is blocked. Type exit and start a new session. '
                              'Use write/edit for file changes; select a clean workspace for bash tests.')
                else:
                    raw_print('disclosure_denied: content cannot be displayed safely.')
            except Exception:
                raw_print('disclosure_denied')
    mode = "Resumed" if events_path.exists() else "New"
    print_fn(f"My Coding Agent (OpenAI SDK) — {mode} chat {events_path.stem[:8]}")
    if getattr(getattr(agent, 'sandbox', None), 'publish_files', False) is True:
        print_fn(f"File changes are saved to your project: {agent.workdir}")
    if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
        print_fn("Live workspace — changes are written directly; review or undo with Git/IDE.")
    print_fn("Type 'exit' to quit. Gửi ảnh: /img path1,path2 lời nhắn\n")
    try:
        handle_pending_runtime_actions(agent, input_fn=input_fn, print_fn=print_fn)
        while True:
            try:
                user_text = input_fn(f"{YELLOW}agent>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                raw_print("\nbye")
                break
            if not user_text:
                continue
            lowered = user_text.lower()
            if lowered in {"exit", "quit"}:
                raw_print("bye")
                break
            if lowered == "/memory":
                print_fn(agent.memory_manager.read())
                continue
            if lowered == "/remember" or lowered.startswith("/remember "):
                try:
                    entry, created = agent.memory_manager.remember(user_text[len("/remember"):].strip())
                    action = "Remembered" if created else "Already remembered"
                    print_fn(f"{action} [{entry.memory_id}] {entry.fact}")
                except (OSError, ValueError) as error:
                    print_fn(f"Memory error: {error}")
                continue
            if lowered == "/forget" or lowered.startswith("/forget "):
                try:
                    entry = agent.memory_manager.forget(user_text[len("/forget"):].strip())
                    print_fn(f"Forgot [{entry.memory_id}] {entry.fact}")
                except (OSError, ValueError) as error:
                    print_fn(f"Memory error: {error}")
                continue
            if lowered == "/changes":
                if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
                    print_fn("Changes are already in the workspace; review them with git diff or your IDE.")
                    continue
                try:
                    changes = agent.prepare_changes()
                    print_fn(_render_changes(changes))
                except (OSError, ValueError, RuntimeError):
                    print_fn('changes_unavailable')
                continue
            if lowered == "/apply":
                if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
                    print_fn("Apply is not needed: successful tool changes are already in the workspace.")
                    continue
                print_fn('Approved file changes are already saved to the project.'
                         if getattr(getattr(agent, 'sandbox', None), 'publish_files', False) is True
                         else 'publication_unavailable: use /changes then /export')
                continue
            if lowered.startswith('/export '):
                name = user_text[len('/export '):].strip()
                if getattr(agent, 'last_changes', None) is None:
                    print_fn('Run /changes before /export')
                    continue
                digest = input_fn('Approve exact change set digest: ').strip()
                try:
                    result = agent.export_changes(name, digest)
                    print_fn(f'Patch exported: {result}')
                except (OSError, ValueError, RuntimeError):
                    print_fn('patch_export_denied')
                continue
            if lowered == "/discard":
                live = getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE
                agent.discard()
                print_fn(
                    "Sandbox closed; live workspace changes remain. Use Git/IDE to undo them."
                    if live or getattr(getattr(agent, 'sandbox', None), 'publish_files', False) is True
                    else "Sandbox discarded."
                )
                break
            if user_text.lower() == "resume" and agent.runtime_state.active_turn_id:
                try:
                    agent.resume_active_turn()
                    handle_pending_runtime_actions(agent, input_fn=input_fn, print_fn=print_fn)
                except KeyboardInterrupt:
                    raw_print("\n[cancelled by user]")
                continue
            if agent.runtime_state.active_turn_id:
                print_fn("An active turn exists. Type 'resume' after resolving pending runtime actions.")
                continue
            image_paths = None
            if user_text.startswith("/img "):
                paths_str, separator, prompt_text = user_text[len("/img "):].partition(" ")
                if not separator or not prompt_text.strip():
                    print_fn("Cách dùng: /img path1,path2 lời nhắn")
                    continue
                image_paths = [path.strip() for path in paths_str.split(",") if path.strip()]
                user_text = prompt_text.strip()
            try:
                agent.run_turn(user_text, image_paths)
                handle_pending_runtime_actions(agent, input_fn=input_fn, print_fn=print_fn)
            except KeyboardInterrupt:
                raw_print("\n[cancelled by user]")
            except Exception as error:
                detail = {
                    'model_timeout': 'model_timeout: provider did not respond within the timeout after bounded retries. Try your request again.',
                    'model_connection_failed': 'model_connection_failed: could not connect to the provider after bounded retries. Check the connection and try again.',
                }.get(str(error), str(error))
                print_fn(f"Runtime failed: {detail}")
    finally:
        agent.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_root = Path(args.state_root).expanduser()
    chats_dir = state_root / "chats"
    try:
        if args.prompt and not args.headless:
            raise ValueError('--prompt requires --headless')
        safety_config = load_safety_config(
            args.host_policy, Path(args.workspace).resolve() / '.agent-safety.json', args.approval_mode,
            'headless' if args.headless else 'interactive', {},
        )
        limits = ResourceLimits(
            cpus=args.cpus,
            memory_bytes=args.memory_mib * 1024**2,
            pids=args.pids,
            tmpfs_bytes=args.tmpfs_mib * 1024**2,
            tool_timeout_seconds=args.tool_timeout,
            workspace_growth_bytes=args.workspace_growth_mib * 1024**2,
        )
        events_path = select_chat_path(args, chats_dir=chats_dir)
        factory_args = (Path(args.workspace), state_root, args.image, limits, args.control_container_id, safety_config, args.export_root, args.allow_patch_export)
        if args.headless:
            agent = _default_agent_factory(events_path, *factory_args)
            try:
                run_headless(agent, args.prompt)
            finally:
                agent.close()
        else:
            run_repl(events_path, factory_args=factory_args)
    except (ValueError, EOFError, KeyboardInterrupt, OSError, RuntimeError) as error:
        print(f"Sandbox setup failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
