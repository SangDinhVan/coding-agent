from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from tools.base import BaseTool, ToolResult


@dataclass
class FakeDelta:
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)


def _chunk(delta: FakeDelta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def stream_text(text: str):
    return [_chunk(FakeDelta(content=text))]


def stream_tool_call(call_id: str, name: str, arguments: str = "{}"):
    call = SimpleNamespace(
        index=0,
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    return [_chunk(FakeDelta(tool_calls=[call]))]


class FakeTool(BaseTool):
    name = "fake"
    description = "Deterministic fake tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, result: ToolResult | None = None, error: Exception | None = None):
        self.result = result or ToolResult("raw-ok", "compact-ok", True)
        self.error = error
        self.calls = 0

    def execute(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result
