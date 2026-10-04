import json
import os
import stat
from pathlib import Path

from openai import OpenAI

from core.config import MODEL, BASE_URL, CONTEXT_WINDOW, API_KEY


# ponytail: CLI turns are serial; switch to contextvars if concurrent turns are added.
_trace_path: Path | None = None
_trace_call_number = 0


def _open_trace(path, mode):
    from core.paths import open_directory
    parent = open_directory(path.parent, create=True)
    try:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError('unsafe_trace_file')
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, mode, encoding='utf-8')
    except BaseException:
        os.close(fd)
        raise


def start_trace(path: str | Path) -> Path:
    global _trace_path, _trace_call_number
    path = Path(path)
    with _open_trace(path, 'r') as trace:
        _trace_call_number = sum(line.startswith("lần gọi thứ:") for line in trace)
    _trace_path = path
    return _trace_path


def finish_trace() -> Path | None:
    global _trace_path, _trace_call_number
    path = _trace_path
    _trace_path = None
    _trace_call_number = 0
    return path


def append_trace_event(event: str, fields: dict, disclosure_check=None) -> None:
    if _trace_path is None:
        return
    from runtime.safety import DisclosureDenied, screen_artifact

    block = '[runtime] ' + json.dumps({'event': event, **fields}, ensure_ascii=False) + '\n'
    if screen_artifact(block, '', (API_KEY,))['secret_exposure_detected']:
        return
    if disclosure_check:
        try:
            disclosure_check(block, 'trace')
        except DisclosureDenied:
            return
    with _open_trace(_trace_path, 'a') as trace:
        trace.write(block)
        trace.flush()
        os.fsync(trace.fileno())


def _usage_tokens(usage) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    get = usage.get if isinstance(usage, dict) else lambda key: getattr(usage, key, None)
    input_tokens = get("prompt_tokens")
    if input_tokens is None:
        input_tokens = get("input_tokens")
    output_tokens = get("completion_tokens")
    if output_tokens is None:
        output_tokens = get("output_tokens")
    return input_tokens, output_tokens


def _append_trace(call_number: int, request: dict, output_text: str, usage, disclosure_check=None) -> None:
    if _trace_path is None:
        return
    if disclosure_check:
        disclosure_check(json.dumps({'request': request, 'output': output_text}, ensure_ascii=False), 'trace')
    provider_input, provider_output = _usage_tokens(usage)
    input_tokens = provider_input
    input_source = "provider"
    if input_tokens is None:
        input_tokens = count_tokens(json.dumps(request, ensure_ascii=False), request["model"])
        input_source = "ước tính"
    output_tokens = provider_output
    output_source = "provider"
    if output_tokens is None:
        output_tokens = count_tokens(output_text, request["model"])
        output_source = "ước tính"
    block = (
        f"lần gọi thứ: {call_number}\n"
        f"token input: {input_tokens} ({input_source})\n"
        f"token output: {output_tokens} ({output_source})\n"
        "context:\n"
        f"{json.dumps(request, ensure_ascii=False, indent=2, default=str)}\n\n"
    )
    with _open_trace(_trace_path, 'a') as trace:
        trace.write(block)
        trace.flush()
        os.fsync(trace.fileno())


def _next_trace_call() -> int | None:
    global _trace_call_number
    if _trace_path is None:
        return None
    _trace_call_number += 1
    return _trace_call_number


def _trace_stream(response, call_number: int | None, request: dict, disclosure_check=None):
    output = []
    usage = None
    total = 0
    try:
        for chunk in response:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    output.append(content)
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    output.extend((
                        getattr(tool_call, "id", None) or "",
                        getattr(tool_call, "type", None) or "",
                    ))
                    function = getattr(tool_call, "function", None)
                    if function is not None:
                        output.extend((
                            getattr(function, "name", None) or "",
                            getattr(function, "arguments", None) or "",
                        ))
            total = sum(len(text.encode()) for text in output)
            if total > 32 * 1024**2:
                raise RuntimeError('model_output_limit')
            yield chunk
    finally:
        if call_number is not None:
            _append_trace(call_number, request, "".join(output), usage, disclosure_check)


def get_context_window(model: str = None) -> int:
    return CONTEXT_WINDOW


def complete(
    messages,
    tools=None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    stream: bool = False,
    disclosure_check=None,
    disclosure_sink='main_model',
    **kwargs,
):
    client_options = {
        "api_key": api_key or API_KEY,
        "base_url": base_url or BASE_URL,
        "timeout": kwargs.pop("timeout", 120),
        "max_retries": kwargs.pop("max_retries", 2),
    }

    request = {
        "model": model or MODEL,
        "messages": messages,
        "stream": stream,
        **kwargs,
    }
    if stream:
        request.setdefault("stream_options", {"include_usage": True})
    if tools is not None:
        request["tools"] = tools

    if disclosure_check:
        disclosure_check(json.dumps(request, ensure_ascii=False), disclosure_sink)
    call_number = _next_trace_call()
    try:
        response = OpenAI(**client_options).chat.completions.create(**request)
    except BaseException as error:
        if disclosure_check:
            disclosure_check(str(error), 'audit_text')
        if call_number is not None:
            _append_trace(call_number, request, "", None, disclosure_check)
        raise
    if stream:
        return _trace_stream(response, call_number, request, disclosure_check)
    if call_number is None and disclosure_check is None:
        return response
    message = response.choices[0].message
    output = content_to_text(message.content or "")
    if getattr(message, "tool_calls", None):
        output += str(message.tool_calls)
    if disclosure_check:
        disclosure_check(output, disclosure_sink)
    if call_number is not None:
        _append_trace(call_number, request, output, getattr(response, "usage", None), disclosure_check)
    return response


def complete_text(prompt: str, model: str = None, base_url: str = None, api_key: str = None, **kwargs) -> str:

    response = complete(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        stream=False,
        base_url=base_url,
        api_key=api_key,
        **kwargs,
    )
    return response.choices[0].message.content or ""


def content_to_text(content) -> str:
    """
    Chuyển content (string hoặc list OpenAI content parts — dạng có ảnh) về
    text thuần, để đếm token / render tóm tắt không phải nhúng base64 ảnh.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(texts)
    return str(content)


def count_tokens(text: str, model: str = None) -> int:
    text = content_to_text(text)
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4


def count_messages_tokens(messages: list[dict], model: str = None) -> int:
    """Đếm tổng token của toàn bộ messages (cộng dồn content + tool_calls)."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, (str, list)):
            total += count_tokens(content, model)
        if msg.get("tool_calls"):
            total += count_tokens(str(msg["tool_calls"]), model)
    return total
