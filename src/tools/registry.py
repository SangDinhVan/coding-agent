from tools.base import BaseTool
from tools.filesystem import ReadTool, WriteTool, EditTool
from tools.terminal import BashTool

_TOOLS: list[BaseTool] = [
    ReadTool(),
    WriteTool(),
    EditTool(),
    BashTool(),
]

_TOOLS_BY_NAME: dict[str, BaseTool] = {tool.name: tool for tool in _TOOLS}

def get_schemas() -> list[dict]:
    return [tool.schema() for tool in _TOOLS]


def get_tool(name: str) -> BaseTool | None:
    return _TOOLS_BY_NAME.get(name)