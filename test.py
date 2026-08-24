import json
import re
import os
from pathlib import Path
import subprocess
from dotenv import load_dotenv
import litellm

load_dotenv()

# MODEL = "openai/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF"
# BASE_URL = "https://interfaces-apt-dallas-westminster.trycloudflare.com/v1"
# API_KEY = "sk-sang-huy-diet-2026#"

API_KEY = os.environ["API_KEY"]
MODEL = "openai/deepseek/deepseek-v4-pro"
BASE_URL = "https://ai-gateway.inter-k.com/v1"

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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fallback parser: một số model GGUF fine-tune tự sinh cú pháp tag riêng
# dạng <function=NAME><parameter=KEY>VALUE</parameter>...</function>
# thay vì trả về field `tool_calls` chuẩn OpenAI. Hàm này bóc tách format đó.
# ---------------------------------------------------------------------------

FUNCTION_RE = re.compile(r"<function=(\w+)>(.*?)</function>", re.S)
PARAMETER_RE = re.compile(r"<parameter=(\w+)>\n?(.*?)\n?</parameter>", re.S)


def parse_custom_tool_calls(text: str):
    """Parse format: <function=NAME><parameter=KEY>VALUE</parameter>...</function>"""
    calls = []
    for func_match in FUNCTION_RE.finditer(text):
        name = func_match.group(1)
        body = func_match.group(2)
        args = {}
        for param_match in PARAMETER_RE.finditer(body):
            key = param_match.group(1)
            val = param_match.group(2)
            # Không strip nội dung file/code để tránh mất indentation/newline có chủ đích
            if key not in ("content", "old_text", "new_text", "command"):
                val = val.strip()
            args[key] = val
        calls.append({"name": name, "arguments": args})
    return calls


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

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

        # -------------------------------------------------------------
        # Trường hợp 1: model trả tool_calls chuẩn OpenAI -> xử lý như cũ
        # -------------------------------------------------------------
        if tool_calls:
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
                print(f"tool result> {result[:500]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    }
                )
            continue  # gọi lại model với kết quả tool

        # -------------------------------------------------------------
        # Trường hợp 2: model không trả tool_calls chuẩn, nhưng content
        # có chứa cú pháp tag <function=...> -> fallback parser
        # -------------------------------------------------------------
        fallback_calls = parse_custom_tool_calls(content) if "<function=" in content else []
        if fallback_calls:
            # Lưu lại nguyên văn câu trả lời của model (bao gồm cả tag)
            messages.append({"role": "assistant", "content": content})

            for call in fallback_calls:
                print(f"tool> {call['name']}({call['arguments']})")
                result = execute_tool(call["name"], call["arguments"])
                print(f"tool result> {result[:500]}")
                # Model không tự sinh tool_call_id nên không dùng role "tool" chuẩn được,
                # đẩy kết quả về dạng user message để model đọc và tiếp tục ở lượt sau
                messages.append(
                    {
                        "role": "user",
                        "content": f"[Tool result - {call['name']}]: {result}",
                    }
                )
            continue  # gọi lại model với kết quả tool

        # -------------------------------------------------------------
        # Trường hợp 3: không có tool call nào -> kết thúc lượt
        # -------------------------------------------------------------
        messages.append({"role": "assistant", "content": content})
        return


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