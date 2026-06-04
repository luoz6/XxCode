"""Built-in read-only tools for MCP resource discovery and reading."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...mcp.client import ConnectionState, McpClient, McpError
from ...tools import Tool
from .schema import _EmptyInput


class ReadMcpResourceInput(BaseModel):
    server_name: str = Field(description="MCP server name")
    uri: str = Field(description="Resource URI to read")


class ListMcpResourcesTool(Tool):
    """List available resources from all connected MCP servers."""

    name = "mcp_list_resources"
    description = (
        "List all available resources from connected MCP servers. "
        "Resources can include files, data sets, API endpoints, etc. "
        "Use this to discover what data sources are available, then use "
        "mcp_read_resource to fetch specific resources."
    )
    input_schema = _EmptyInput
    aliases = ["list_mcp_resources"]

    _is_concurrency_safe = True
    _is_read_only = True
    _is_destructive = False

    async def execute(self, input: BaseModel, context: dict[str, Any]) -> str:
        clients: dict[str, McpClient] = context.get("mcp_clients", {})
        if not clients:
            return "No MCP servers are connected."

        lines: list[str] = []
        for name, client in clients.items():
            if client.state != ConnectionState.CONNECTED:
                lines.append(f"[{name}] (disconnected)")
                continue
            try:
                resources = await client.discover_resources()
                if not resources:
                    lines.append(f"[{name}] No resources available")
                for resource in resources:
                    uri = resource.get("uri", "?")
                    desc = resource.get("name", resource.get("description", uri))
                    mime = f" ({resource.get('mimeType')})" if resource.get("mimeType") else ""
                    lines.append(f"[{name}] {desc}{mime} - {uri}")
            except McpError as e:
                lines.append(f"[{name}] Error listing resources: {e.message}")

        if not lines:
            return "No MCP resources available."
        return "\n".join(lines)

    def render_tool_use(self, input: BaseModel) -> str:
        return "MCP: list_resources"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"MCP list_resources failed: {content[:100]}"
        count = content.count("\n") + 1 if content else 0
        return f"MCP resources ({count} entries)"


class ReadMcpResourceTool(Tool):
    """Read a specific MCP resource by URI."""

    name = "mcp_read_resource"
    description = (
        "Read the content of a specific MCP resource by its URI. "
        "Use mcp_list_resources first to discover available resources, "
        "then use this tool to fetch the content of one you need."
    )
    input_schema = ReadMcpResourceInput
    aliases = ["read_mcp_resource"]

    _is_concurrency_safe = True
    _is_read_only = True
    _is_destructive = False

    async def execute(self, input: ReadMcpResourceInput, context: dict[str, Any]) -> str:
        clients: dict[str, McpClient] = context.get("mcp_clients", {})
        client = clients.get(input.server_name)
        if client is None:
            return (
                f"<tool_use_error>\n"
                f"MCP server '{input.server_name}' is not connected.\n"
                f"</tool_use_error>"
            )
        try:
            return await client.read_resource(input.uri)
        except McpError as e:
            return (
                f"<tool_use_error>\n"
                f"Error reading resource '{input.uri}' on server "
                f"'{input.server_name}': [{e.code}] {e.message}\n"
                f"</tool_use_error>"
            )

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, ReadMcpResourceInput)
        return f"MCP: read {input.uri}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"MCP read_resource failed: {content[:100]}"
        return f"MCP resource ({len(content)} chars)"


__all__ = [
    "ListMcpResourcesTool",
    "ReadMcpResourceInput",
    "ReadMcpResourceTool",
]
