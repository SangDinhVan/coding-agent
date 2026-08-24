from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


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


class BaseTool(ABC):

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
        required = self.parameters.get("required", [])
        missing = [field for field in required if field not in kwargs]

        if missing:
            return f"Missing required field(s): {', '.join(missing)}"

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