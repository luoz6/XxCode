"""Tool registry — discovers, validates, and dispatches tool calls."""

from typing import Any

from . import Tool, ToolCall, ToolResult


class ToolRegistry:
    """Registry of all available tools.

    Provides lookup by name and dispatches tool calls to the correct executor.
    """

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_api_schemas(self) -> list[dict[str, Any]]:
        """Return API-ready tool schemas for all registered tools."""
        return [tool.to_api_schema() for tool in self._tools.values()]

    def get_read_only_tools(self) -> list[Tool]:
        """Return only read-only tools."""
        return [t for t in self._tools.values() if t.is_read_only()]

    async def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        """Execute a tool call and return the result.

        Args:
            call: The tool call to execute.
            context: Execution context dict (cwd, config, etc.)

        Returns:
            ToolResult with the output or error message.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_use_id=call.id,
                content=f"Error: Unknown tool '{call.name}'. Available: {', '.join(self._tools.keys())}",
                is_error=True,
            )

        try:
            validated_input = tool.input_schema.model_validate(call.input)
        except Exception as e:
            return ToolResult(
                tool_use_id=call.id,
                content=f"Error: Invalid input for tool '{call.name}': {e}",
                is_error=True,
            )

        try:
            output = await tool.execute(validated_input, context)
            return ToolResult(tool_use_id=call.id, content=output)
        except Exception as e:
            return ToolResult(
                tool_use_id=call.id,
                content=f"Error executing '{call.name}': {e}",
                is_error=True,
            )
