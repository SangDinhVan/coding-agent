from tools.base import BaseTool
from tools.filesystem import ReadTool, WriteTool, EditTool
from tools.terminal import BashTool


class ToolRegistry:
    def __init__(self, sandbox):
        self._tools = [ReadTool(sandbox), WriteTool(sandbox), EditTool(sandbox), BashTool(sandbox)]
        self._by_name = {tool.name: tool for tool in self._tools}

    def schemas(self):
        return [tool.schema() for tool in self._tools]

    def get(self, name):
        return self._by_name.get(name)


# Compatibility-only unbound registry. Every OS tool fails closed.
_COMPAT = ToolRegistry(None)

def get_schemas() -> list[dict]: return _COMPAT.schemas()
def get_tool(name: str) -> BaseTool | None: return _COMPAT.get(name)
