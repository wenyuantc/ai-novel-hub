"""项目智能体工具注册表。只暴露白名单工具，不动态导入模型指定的名称。"""

from .base import Tool, ToolCtx
from .read import TOOLS as READ_TOOLS
from .tasks import TOOLS as TASK_TOOLS
from .write import TOOLS as WRITE_TOOLS
from .write_text import TOOLS as TEXT_TOOLS

_TOOLS = {tool.name: tool for tool in [*READ_TOOLS, *WRITE_TOOLS, *TEXT_TOOLS, *TASK_TOOLS]}


def get_tool(name: str) -> Tool | None:
    return _TOOLS.get(name)


def list_tools() -> list[Tool]:
    return list(_TOOLS.values())


__all__ = ["Tool", "ToolCtx", "get_tool", "list_tools"]
