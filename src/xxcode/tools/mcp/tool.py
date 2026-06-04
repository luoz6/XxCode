"""Backward-compatible exports for MCP tool wrappers.

The implementation lives in focused modules:
  - schema.py
  - dynamic_tool.py
  - resource_tools.py
"""

from .dynamic_tool import McpTool
from .resource_tools import (
    ListMcpResourcesTool,
    ReadMcpResourceInput,
    ReadMcpResourceTool,
)
from .schema import _EmptyInput, build_mcp_input_model

__all__ = [
    "McpTool",
    "build_mcp_input_model",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "ReadMcpResourceInput",
    "_EmptyInput",
]
