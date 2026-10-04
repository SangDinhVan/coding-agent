from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from runtime.models import ReplayPolicy


def validate_schema(value, schema: dict) -> str | None:
    branches = schema.get('oneOf')
    if branches is not None:
        return None if sum(validate_schema(value, branch) is None for branch in branches) == 1 else 'invalid schema branch'
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'integer': int, 'number': (int, float)}
    expected = schema.get('type')
    if expected in types and (not isinstance(value, types[expected]) or (expected in {'integer', 'number'} and isinstance(value, bool))):
        return f'expected {expected}'
    if 'const' in schema and value != schema['const']:
        return 'invalid constant'
    if 'enum' in schema and value not in schema['enum']:
        return 'invalid enum'
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if value.keys() - properties.keys():
            return 'unexpected field'
        missing = set(schema.get('required', [])) - value.keys()
        if missing:
            return 'Missing required field(s): ' + ', '.join(sorted(missing))
        for key, item in value.items():
            error = validate_schema(item, properties[key])
            if error:
                return f'{key}: {error}'
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            return 'too few items'
        for item in value:
            error = validate_schema(item, schema.get('items', {}))
            if error:
                return error
    if isinstance(value, str) and len(value) < schema.get('minLength', 0):
        return 'string too short'
    return None


@dataclass(frozen=True)
class ToolResult:
    """
    Kết quả trả về từ 1 lần gọi tool.

    raw:      output đầy đủ, không rút gọn. Dùng để ghi xuống events.jsonl
              (raw history không bao giờ đổi — nguyên tắc đã chốt trong plan).
    compact:  bản rút gọn, đây là cái sẽ được nhét vào messages gửi cho model.
              Tool tự biết cách nén output của chính nó tốt nhất.
    success:  True/False — để agent.py / TurnState biết tool này chạy ổn hay lỗi
              mà không cần đoán qua nội dung string.
    """
    raw: str
    compact: str
    success: bool
    exit_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):

    replay_policy: ReplayPolicy = ReplayPolicy.MANUAL
    name: str
    description: str
    parameters: dict  

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, kwargs: dict) -> Optional[str]:
        """
        Check nhanh field bắt buộc có đủ không, dựa vào self.parameters["required"].
        """
        return validate_schema(kwargs, self.parameters)

    def recovery_metadata(self, **kwargs: Any) -> dict[str, Any]:
        """Return facts persisted before a side effect; empty for non-reconcilable tools."""
        return {}

    def recovery_fingerprint(self, metadata: dict[str, Any]) -> str | None:
        """Probe current effect state through the tool's execution boundary."""
        return None

    def run(self, **kwargs: Any) -> ToolResult:
        """
        Entry point duy nhất mà agent.py nên gọi (KHÔNG gọi thẳng execute()).
        """
        error = self.validate(kwargs)
        if error is not None:
            return ToolResult(
                raw=f"ValidationError: {error}",
                compact=f"Invalid tool call for '{self.name}': {error}",
                success=False,
            )

        return self.execute(**kwargs)

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError
