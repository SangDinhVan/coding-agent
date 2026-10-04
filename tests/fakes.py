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


def safety_context(session_id):
    from runtime.safety import AuthorityRecord

    return {
        'authority': AuthorityRecord('test', session_id, 'a' * 64, 'private',
                                     ('fake', 'read', 'write', 'edit', 'bash'), ('ui', 'main_model', 'reviewer', 'summarizer', 'memory', 'audit_text', 'trace', 'patch_export')),
        'generation_digest': 'b' * 64, 'scope': 'private',
        'image_digest': 'sha256:' + 'c' * 64, 'environment_digest': 'd' * 64,
        'capabilities': {'profile_id': 'shadow', 'scope': 'private', 'internal_tools': ('fake',), 'file_adapter': True, 'opaque_ro': True},
        'checkpoint_action': lambda action: 'e' * 64,
    }


def trusted_executor(store, **kwargs):
    from runtime.executor import ToolExecutor

    kwargs['policy'] = kwargs.get('policy') or (lambda execution: 'allow')
    kwargs.setdefault('get_security_context', lambda: safety_context(store.session_id))
    return ToolExecutor(store, **kwargs)


def trusted_agent(*args, **kwargs):
    from agent.loop import Agent
    from pathlib import Path

    events = kwargs.get('events_path') or args[0]
    kwargs.setdefault('get_security_context', lambda: safety_context(Path(events).stem))
    return Agent(*args, **kwargs)
