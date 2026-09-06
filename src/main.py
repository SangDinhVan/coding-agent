import argparse
from datetime import datetime
from pathlib import Path
from typing import Callable

from memory.event_store import DEFAULT_CHATS_DIR, create_chat_path, list_chats, resolve_chat

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


def run_repl(events_path: Path) -> None:
    from agent.loop import Agent
    from core.config import MODEL

    agent = Agent(model=MODEL, workdir=".", events_path=str(events_path))
    mode = "Resumed" if events_path.exists() else "New"
    print(f"My Coding Agent (LiteLLM) — {mode} chat {events_path.stem[:8]}")
    print("Type 'exit' to quit. Gửi ảnh: /img path1,path2 lời nhắn\n")

    while True:
        try:
            user_text = input(f"{YELLOW}agent>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit"}:
            print("bye")
            break

        image_paths = None
        if user_text.startswith("/img "):
            paths_str, separator, prompt_text = user_text[len("/img "):].partition(" ")
            if not separator or not prompt_text.strip():
                print("Cách dùng: /img path1,path2 lời nhắn")
                continue
            image_paths = [path.strip() for path in paths_str.split(",") if path.strip()]
            user_text = prompt_text.strip()

        try:
            agent.run_turn(user_text, image_paths)
        except KeyboardInterrupt:
            print("\n[cancelled by user]")


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
