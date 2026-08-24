from agent.loop import Agent
from core.config import MODEL

agent = Agent(model=MODEL, workdir=".")

if __name__ == "__main__":
    YELLOW = "\033[93m"
    RESET = "\033[0m"
    print("My Coding Agent (LiteLLM). Type 'exit' to quit.")
    print("Gửi ảnh kèm câu hỏi: /img path1,path2 lời nhắn\n")
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
            rest = user_text[len("/img "):]
            paths_str, _, prompt_text = rest.partition(" ")
            image_paths = [p.strip() for p in paths_str.split(",") if p.strip()]
            user_text = prompt_text

        try:
            agent.run_turn(user_text, image_paths)
        except KeyboardInterrupt:
            print("\n[cancelled by user]")
