import argparse
from datetime import datetime
from pathlib import Path
from typing import Callable

from memory.event_store import DEFAULT_CHATS_DIR, create_chat_path, list_chats, resolve_chat
from runtime.models import RecoveryDecision, ReplayPolicy

YELLOW = "\033[93m"
RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent", description="Personal AI coding agent")
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
            print_fn("Approval must be resolved by the configured approval handler.")
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


def _default_agent_factory(events_path: Path):
    from agent.loop import Agent
    from core.config import MODEL
    return Agent(model=MODEL, workdir=".", events_path=str(events_path))


def run_repl(
    events_path: Path,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    agent_factory: Callable[[Path], object] = _default_agent_factory,
) -> None:
    agent = agent_factory(events_path)
    mode = "Resumed" if events_path.exists() else "New"
    print_fn(f"My Coding Agent (LiteLLM) — {mode} chat {events_path.stem[:8]}")
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
            if user_text.lower() in {"exit", "quit"}:
                print_fn("bye")
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
    try:
        events_path = select_chat_path(args)
    except (ValueError, EOFError, KeyboardInterrupt) as error:
        print(f"Không thể resume: {error}")
        return 1
    run_repl(events_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
