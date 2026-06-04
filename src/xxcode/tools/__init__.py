"""XxCode 工具系统 — 公共 API 入口。

所有核心契约类从 base.py 导入并在此处重新导出，保持向后兼容。
外部代码应从此模块（而非 base.py）导入：from xxcode.tools import Tool
"""

# Re-export core contracts from base.py
from .base import (
    TOOL_DEFAULTS,
    Tool,
    ToolCall,
    ToolResult,
    build_tool,
)

# Lazy import for AgentTool (circular dependency avoidance)
# AgentTool is imported here to keep the public API flat, but it's
# defined in tools/agent/tool.py to avoid coupling base.py to agent infra.


def _get_agent_tool():
    """Lazy loader for AgentTool — avoids import-time circular deps."""
    from xxcode.tools.agent.tool import AgentInput, AgentTool as _AgentTool
    return _AgentTool, AgentInput


def __getattr__(name: str):
    """Defer AgentTool/AgentInput imports to first access."""
    if name == "AgentTool":
        return _get_agent_tool()[0]
    if name == "AgentInput":
        return _get_agent_tool()[1]
    raise AttributeError(f"module 'xxcode.tools' has no attribute '{name}'")


__all__ = [
    "Tool",
    "ToolCall",
    "ToolResult",
    "TOOL_DEFAULTS",
    "build_tool",
    "AgentTool",
    "AgentInput",
]
