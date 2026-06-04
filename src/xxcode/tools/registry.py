"""Tool registry: lookup, schema export, and tool-call dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from . import Tool, ToolCall, ToolResult, build_tool
from .deferred import DeferredToolIndex

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of active and deferred tools."""

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        self._deferred = DeferredToolIndex()
        self._aliases: dict[str, str] = {}
        self._deprecated: dict[str, str] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a tool instance.

        Deferred tools stay hidden until activated through ToolSearchTool.
        """
        if getattr(tool, "_should_defer", False):
            self._deferred.add(tool)
            return
        self._register_active(tool, tool.name)

    def register_class(self, tool_cls: type[Tool], **kwargs) -> Tool:
        """Instantiate a tool with build_tool() and register it."""
        instance = build_tool(tool_cls, **kwargs)
        self.register(instance)
        return instance

    def get(self, name: str) -> Tool | None:
        """Get a tool by canonical name or alias."""
        if name in self._tools:
            return self._tools[name]

        canonical = self._aliases.get(name)
        if canonical is not None:
            if name in self._deprecated:
                logger.warning(
                    "Tool '%s' is deprecated: %s - use '%s' instead.",
                    name,
                    self._deprecated[name],
                    canonical,
                )
            return self._tools.get(canonical)
        return None

    def resolve_name(self, name: str) -> tuple[str | None, str | None]:
        """Resolve a tool name or alias to ``(canonical, warning)``."""
        if name in self._tools:
            return name, None
        canonical = self._aliases.get(name)
        if canonical is not None:
            return canonical, self._deprecated.get(name)
        return None, None

    def enrich_for_render(self, call: ToolCall, context: dict[str, Any]) -> BaseModel:
        """Return a UI-enriched input copy without mutating API parameters."""
        canonical, _deprecation = self.resolve_name(call.name)
        tool = self._tools.get(canonical) if canonical else None
        if tool is None:
            return _FallbackInput(raw=call.input)

        try:
            validated = tool.input_schema.model_validate(call.input)
        except Exception:
            return _FallbackInput(raw=call.input)

        try:
            return tool.backfill_observable_input(validated, context)
        except Exception:
            logger.debug(
                "backfill_observable_input failed for %s - falling back to validated",
                tool.name,
            )
            return validated

    def list_tools(self) -> list[Tool]:
        """Return all active tools."""
        return list(self._tools.values())

    def filtered_copy(
        self,
        *,
        allow_list: set[str] | frozenset[str] | None = None,
        deny_list: set[str] | frozenset[str] | None = None,
        read_only_only: bool = False,
        enabled_only: bool = False,
    ) -> "ToolRegistry":
        """Return a new registry view containing only matching active tools."""
        filtered = ToolRegistry()
        for tool in self._tools.values():
            if enabled_only and not tool.is_enabled():
                continue
            if allow_list is not None and tool.name not in allow_list:
                continue
            if deny_list and tool.name in deny_list:
                continue
            if read_only_only and not tool.is_read_only():
                continue
            filtered.register(tool)
        return filtered

    def get_deferred_tools(self) -> dict[str, Tool]:
        """Return deferred tools hidden from default API schemas."""
        return self._deferred.get_all()

    def activate_tool(self, name: str) -> Tool | None:
        """Move a deferred tool into the active registry."""
        tool = self._deferred.activate(name)
        if tool is not None:
            self._register_active(tool, name)
        return tool

    def search_deferred(self, query: str) -> list[Tool]:
        """Search deferred tools by name/search hint."""
        return self._deferred.search(query)

    def get_api_schemas(
        self,
        *,
        enabled_only: bool = True,
        deny_list: set[str] | None = None,
        read_only_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return deterministic API-ready tool schemas."""
        tools = list(self._tools.values())
        if enabled_only:
            tools = [tool for tool in tools if tool.is_enabled()]
        if deny_list:
            tools = [tool for tool in tools if tool.name not in deny_list]
        if read_only_only:
            tools = [tool for tool in tools if tool.is_read_only()]

        tools.sort(key=lambda tool: tool.name)
        return [tool.to_api_schema() for tool in tools]

    def get_read_only_tools(self) -> list[Tool]:
        """Return active tools marked read-only."""
        return [tool for tool in self._tools.values() if tool.is_read_only()]

    async def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        """Execute a tool call through lookup, validation, and timeout wrapping.

        Failures are returned as ToolResult(is_error=True) so exceptions do not
        escape into the agent loop.
        """
        canonical, deprecation_warning = self.resolve_name(call.name)
        tool = self._tools.get(canonical) if canonical else None
        if tool is None:
            available = ", ".join(sorted(self._tools.keys()))
            return ToolResult(
                tool_use_id=call.id,
                content=(
                    f"<tool_use_error>\n"
                    f"Unknown tool: '{call.name}'.\n"
                    f"Available tools: {available}\n"
                    f"Please use one of the available tools listed above.\n"
                    f"</tool_use_error>"
                ),
                is_error=True,
            )

        try:
            validated_input = tool.input_schema.model_validate(call.input)
        except Exception as exc:
            return ToolResult(
                tool_use_id=call.id,
                content=(
                    f"<tool_use_error>\n"
                    f"Invalid input for tool '{tool.name}'.\n"
                    f"Error: {exc}\n"
                    f"The tool expects the following schema:\n"
                    f"  {tool.input_schema.model_json_schema()}\n"
                    f"Please correct the parameters and try again.\n"
                    f"</tool_use_error>"
                ),
                is_error=True,
            )

        valid, err_msg = await tool.validate_input(validated_input, context)
        if not valid:
            return ToolResult(
                tool_use_id=call.id,
                content=(
                    f"<tool_use_error>\n"
                    f"Validation failed for '{tool.name}': {err_msg}\n"
                    f"Please address the issue above and retry.\n"
                    f"</tool_use_error>"
                ),
                is_error=True,
            )

        timeout = getattr(tool, "timeout_seconds", 30.0)
        try:
            output = await asyncio.wait_for(
                tool.execute(validated_input, context),
                timeout=timeout,
            )
            if deprecation_warning:
                output = f"[Deprecation warning: {deprecation_warning}]\n\n{output}"
            return ToolResult(tool_use_id=call.id, content=output)
        except asyncio.TimeoutError:
            return ToolResult(
                tool_use_id=call.id,
                content=(
                    f"<tool_use_error>\n"
                    f"Tool '{tool.name}' timed out after {timeout:.0f}s.\n"
                    f"The operation took too long to complete. Consider:\n"
                    f"  - Breaking the task into smaller steps.\n"
                    f"  - Using a more targeted query or path.\n"
                    f"  - Increasing the timeout if this is expected to be slow.\n"
                    f"Do NOT retry the exact same call - it will time out again.\n"
                    f"</tool_use_error>"
                ),
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_use_id=call.id,
                content=(
                    f"<tool_use_error>\n"
                    f"Unexpected error executing '{tool.name}': {exc}\n"
                    f"Do NOT repeat the exact same call. Diagnose the error "
                    f"and try an alternative approach.\n"
                    f"</tool_use_error>"
                ),
                is_error=True,
            )

    def _register_active(self, tool: Tool, canonical_name: str) -> None:
        """Register an active tool and all of its aliases."""
        self._tools[canonical_name] = tool
        for alias in tool.aliases:
            self._aliases[alias] = canonical_name
        for alias, warning in tool.deprecated_aliases.items():
            self._aliases[alias] = canonical_name
            self._deprecated[alias] = warning


class _FallbackInput:
    """Lightweight wrapper used when validation/backfill fails."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        if name == "_raw":
            return object.__getattribute__(self, "_raw")
        return self._raw.get(name)

    def __str__(self) -> str:
        return str(self._raw)
