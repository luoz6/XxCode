"""Dynamic Tool wrapper for MCP server tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...mcp.client import McpClient, McpError
from ...tools import Tool
from .schema import _EmptyInput


class McpTool(Tool):
    """Dynamic proxy tool that dispatches to a specific MCP server tool.

    External MCP tools default to fail-closed: they are not read-only and are
    considered destructive unless the user explicitly approves them.
    """

    name: str = ""
    description: str = ""
    input_schema: type[BaseModel] = _EmptyInput
    aliases: list[str] = []

    _is_concurrency_safe = True
    _is_read_only = False
    _is_destructive = True

    _mcp_server_name: str = ""
    _mcp_tool_name: str = ""
    _mcp_tool_schema: dict[str, Any] = {}

    # Let the common tool result budget pipeline handle persistence/preview.
    _max_output_chars = 100_000

    @classmethod
    def from_definition(
        cls,
        *,
        public_name: str,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: type[BaseModel],
        raw_schema: dict[str, Any],
        should_defer: bool = True,
        search_hint: str = "",
    ) -> "McpTool":
        """Create a configured MCP tool wrapper from a discovered tool def."""
        instance = cls()
        instance.name = public_name
        instance.description = description
        instance.input_schema = input_schema
        instance._mcp_server_name = server_name
        instance._mcp_tool_name = tool_name
        instance._mcp_tool_schema = raw_schema
        instance._should_defer = should_defer
        instance._search_hint = search_hint
        return instance

    async def execute(self, input: BaseModel, context: dict[str, Any]) -> str:
        """Dispatch to the MCP client for tool execution."""
        clients: dict[str, McpClient] = context.get("mcp_clients", {})
        client = clients.get(self._mcp_server_name)
        if client is None:
            return (
                f"<tool_use_error>\n"
                f"MCP server '{self._mcp_server_name}' is not connected. "
                f"It may have failed to start or been disconnected.\n"
                f"</tool_use_error>"
            )

        arguments = input.model_dump(exclude_none=True)
        try:
            return await client.call_tool(self._mcp_tool_name, arguments)
        except McpError as e:
            return (
                f"<tool_use_error>\n"
                f"MCP tool '{self._mcp_tool_name}' on server "
                f"'{self._mcp_server_name}' failed: [{e.code}] {e.message}\n"
                f"</tool_use_error>"
            )

    def render_tool_use(self, input: BaseModel) -> str:
        args = input.model_dump(exclude_none=True)
        args_summary = ", ".join(
            f"{key}={str(value)[:40]}" for key, value in args.items()
        ) if args else "no args"
        return f"MCP:{self._mcp_server_name}/{self._mcp_tool_name}({args_summary})"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"MCP failed: {content[:150]}"
        if "Full output saved to:" in content:
            return f"MCP output saved ({len(content)} chars)"
        return f"MCP result ({len(content)} chars)"


__all__ = ["McpTool"]
