import os
import litellm
from core.config import MODEL, BASE_URL, CONTEXT_WINDOW, API_KEY


def get_context_window(model: str = None) -> int:
    return CONTEXT_WINDOW


def complete(messages, tools=None, model: str = None,base_url: str = None,api_key: str = None, stream: bool = False, **kwargs):
    kwargs.setdefault("timeout", 120)
    return litellm.completion(
        model=model or MODEL,
        messages=messages,
        tools=tools,
        stream=stream,
        base_url=base_url or BASE_URL,
        api_key=api_key or API_KEY,
        **kwargs,
    )


def complete_text(prompt: str, model: str = None, base_url: str = None, api_key: str = None) -> str:

    response = complete(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        stream=False,
        base_url=base_url,
        api_key=api_key,
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