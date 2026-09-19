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
    for action in agent.pending_runtime_actions():
        execution = agent.runtime_state.executions[action.execution_id]
        print_fn(f"[{action.kind}] {action.execution_id[:8]} {action.tool_name}: {action.message}")
        if action.kind == "approval":
            while True:
                choice = input_fn("Approval decision (approve/reject): ").strip().lower()
                if choice in {"approve", "reject"}:
                    break
            note = input_fn("Approval note: ").strip()
            agent.resolve_approval(action.execution_id, choice == "approve", note)
            continue
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
        mode=WorkspaceMode.LIVE,
    )
    recover_incomplete_apply(paths, source)
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
    return Agent(
        model=MODEL,
        workdir=str(source),
        events_path=str(events_path),
        sandbox=session,
        tool_registry=ToolRegistry(session),
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
    mode = "Resumed" if events_path.exists() else "New"
    print_fn(f"My Coding Agent (LiteLLM) — {mode} chat {events_path.stem[:8]}")
    if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
        print_fn("Live workspace — changes are written directly; review or undo with Git/IDE.")
    print_fn("Type 'exit' to quit. Gửi ảnh: /img path1,path2 lời nhắn\n")
    try:
        handle_pending_runtime_actions(agent, input_fn=input_fn, print_fn=print_fn)
        while True:
            try:
                user_text = input_fn(f"{YELLOW}agent>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print_fn("\nbye")
                break
            if not user_text:
                continue
            lowered = user_text.lower()
            if lowered in {"exit", "quit"}:
                print_fn("bye")
                break
            if lowered == "/changes":
                if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
                    print_fn("Changes are already in the workspace; review them with git diff or your IDE.")
                    continue
                changes = agent.prepare_changes()
                print_fn(_render_changes(changes))
                continue
            if lowered == "/apply":
                if getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE:
                    print_fn("Apply is not needed: successful tool changes are already in the workspace.")
                    continue
                changes = getattr(agent, "last_changes", None)
                if changes is None or not changes.change_set_hash:
                    print_fn("Run /changes first; blocked change sets cannot be applied.")
                    continue
                approval = input_fn(f"Type 'approve {changes.change_set_hash}' to apply the whole set: ").strip()
                if approval != f"approve {changes.change_set_hash}":
                    print_fn("Apply cancelled: approval hash did not match.")
                    continue
                note = input_fn("Approval note: ").strip()
                if not note:
                    print_fn("Apply cancelled: a non-empty note is required.")
                    continue
                agent.apply_changes(changes.change_set_hash, note)
                print_fn("Change set applied.")
                break
            if lowered == "/discard":
                live = getattr(agent, "workspace_mode", WorkspaceMode.SHADOW) == WorkspaceMode.LIVE
                agent.discard()
                print_fn(
                    "Sandbox closed; live workspace changes remain. Use Git/IDE to undo them."
                    if live else "Sandbox discarded."
                )
                break
            if user_text.lower() == "resume" and agent.runtime_state.active_turn_id:
                try:
                    agent.resume_active_turn()
                except KeyboardInterrupt:
                    print_fn("\n[cancelled by user]")
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
            except KeyboardInterrupt:
                print_fn("\n[cancelled by user]")
            except Exception as error:
                print_fn(f"Runtime failed: {error}")
    finally:
        agent.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_root = Path(args.state_root).expanduser()
    chats_dir = state_root / "chats"
    try:
        limits = ResourceLimits(
            cpus=args.cpus,
            memory_bytes=args.memory_mib * 1024**2,
            pids=args.pids,
            tmpfs_bytes=args.tmpfs_mib * 1024**2,
            tool_timeout_seconds=args.tool_timeout,
            workspace_growth_bytes=args.workspace_growth_mib * 1024**2,
        )
        events_path = select_chat_path(args, chats_dir=chats_dir)
        run_repl(
            events_path,
            factory_args=(Path(args.workspace), state_root, args.image, limits, args.control_container_id),
        )
    except (ValueError, EOFError, KeyboardInterrupt, OSError, RuntimeError) as error:
        print(f"Sandbox setup failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
