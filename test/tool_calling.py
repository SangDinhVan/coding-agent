import json
import os
from pathlib import Path
import subprocess
from dotenv import load_dotenv
import litellm

load_dotenv()

MODEL = "openai/kCode"
BASE_URL = "https://ai-gateway.inter-k.com/v1"
API_KEY = os.environ["API_KEY"]

SYSTEM_PROMPT = (
    "You are a precise coding agent."
    "Use tools when needed. Keep changes minimal and explain clearly."
)

tools = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a UTF-8 text file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write to a UTF-8 text file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exact text in a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"}
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the current directory",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def tool_read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def tool_write(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {path}"


def tool_edit(path: str, old_text: str, new_text: str) -> str:
    p = Path(path)
    content = p.read_text(encoding="utf-8")
    if old_text not in content:
        return f"'{old_text}' not found in {path}"
    new_content = content.replace(old_text, new_text)
    p.write_text(new_content, encoding="utf-8")
    return f"replaced '{old_text}' with '{new_text}' in {path}"


def tool_bash(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=30
    )
    output = (result.stdout or "") + (result.stderr or "")
    return output[:8000] or "(no output)"


def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "read":
            return tool_read(args["path"])
        if name == "write":
            return tool_write(args["path"], args["content"])
        if name == "edit":
            return tool_edit(args["path"], args["old_text"], args["new_text"])
        if name == "bash":
            return tool_bash(args["command"])
        return f"unknown tool: {name}"
    except Exception as e:
        return f"tool error: {e}"


def run_agent(user_request: str):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_request},
    ]

    while True:
        stream = litellm.completion(
            base_url=BASE_URL,
            model=MODEL,
            messages=messages,
            tools=tools,
            stream=True,
            api_key=API_KEY
        )

        content = ""
        tool_calls = {}  # index -> {id, name, arguments}
        printed_prefix = False
        reasoning = ""
        printed_reasoning_prefix = False
        for chunk in stream:
            delta = chunk.choices[0].delta

            reasoning_chunk = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning_chunk:
                if not printed_reasoning_prefix:
                    print("\n\033[90mreasoning> ", end="", flush=True)  # màu xám
                    printed_reasoning_prefix = True
                print(reasoning_chunk, end="", flush=True)
                reasoning += reasoning_chunk

            if delta.content:
                if printed_reasoning_prefix:
                    print("\033[0m")  # reset màu + xuống dòng sau khi hết phần reasoning
                    printed_reasoning_prefix = False
                if not printed_prefix:
                    print("\nassistant> ", end="", flush=True)
                    printed_prefix = True
                print(delta.content, end="", flush=True)
                content += delta.content

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": tc.id, "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls[idx]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls[idx]["arguments"] += tc.function.arguments

        if printed_prefix:
            print()  # xuống dòng sau khi in xong content stream

        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
            return

        # build message.tool_calls dạng chuẩn để lưu vào history
        assistant_tool_calls = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in tool_calls.values()
        ]
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": assistant_tool_calls,
            }
        )

        for tool_call in assistant_tool_calls:
            name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"] or "{}")
            print(f"tool> {name}({args})")
            result = execute_tool(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": result,
                }
            )


if __name__ == "__main__":
    YELLOW = "\033[93m"
    RESET = "\033[0m"
    print("My Coding Agent (LiteLLM). Type 'exit' to quit.\n")
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
        run_agent(user_text)