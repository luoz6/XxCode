"""ToolSearchTool — dynamic tool discovery and lazy loading.

When the tool ecosystem grows (MCP servers, specialized editors, etc.),
sending every tool schema on every API call bloats the system prompt and
breaks prompt caching.  Tools marked _should_defer=True are hidden from
the initial tool list.  The model discovers them via this tool and loads
them on demand with the select:Name mode.

Query modes:
  - select:Name1,Name2  → activate tools, return their full schemas
  - +prefix keywords    → filter by name prefix, rank by keywords
  - keyword1 keyword2   → search all deferred tools, rank by relevance
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from . import Tool
from .registry import ToolRegistry


class ToolSearchInput(BaseModel):
    """Query for discovering and loading deferred tools.

    Three query syntaxes are supported:
      - "select:Name1,Name2" — directly load specific tools by name
      - "+prefix keyword"     — filter by name prefix, then keyword ranking
      - "keyword1 keyword2"   — keyword search across all deferred tools
    """
    query: str = Field(description=(
        "Search query. Use 'select:Name1,Name2' to load specific tools, "
        "'+prefix keyword' to filter by name prefix, or just keywords "
        "to search all available deferred tools."
    ))


class ToolSearchTool(Tool):
    """Discover and load tools that are hidden from the default tool list.

    Some specialized tools (MCP integrations, notebook editors, etc.) are
    deferred to keep the initial tool list small and cache-friendly.  Use
    this tool to find and activate them when needed.
    """

    name = "tool_search"
    description = (
        "Search for and load tools that are not in the default tool list. "
        "Use this when you need a specific tool that isn't available.\n\n"
        "Query syntax:\n"
        '  "select:Name1,Name2" — directly load specific tools by exact name\n'
        '  "+prefix keyword" — filter tools whose name starts with prefix, ranked by keyword relevance\n'
        '  "keyword1 keyword2" — search all deferred tools by name and description\n\n'
        "After loading with select:, the tool becomes available for immediate use."
    )
    input_schema = ToolSearchInput
    aliases = ["search_tools"]

    _is_concurrency_safe = True
    _is_read_only = True   # Loading tools modifies only internal registry state
    _is_destructive = False

    async def execute(self, input: ToolSearchInput, context: dict[str, Any]) -> str:
        registry: ToolRegistry | None = context.get("_registry")
        if registry is None:
            return "<tool_use_error>\nTool registry not available in context.\n</tool_use_error>"

        query = input.query.strip()

        # ── select: mode — activate specific tools ────────────────
        if query.startswith("select:"):
            return self._handle_select(query, registry)

        # ── +prefix / keyword mode — search deferred ─────────────
        return self._handle_search(query, registry)

    def _handle_select(self, query: str, registry: ToolRegistry) -> str:
        """Activate tools by exact name match."""
        names = [n.strip() for n in query[7:].split(",") if n.strip()]
        if not names:
            return "No tool names specified. Use 'select:Name1,Name2' to load specific tools."

        lines: list[str] = []
        activated: list[str] = []
        not_found: list[str] = []
        already_active: list[str] = []

        for name in names:
            # Check if already active.
            if registry.get(name) is not None:
                already_active.append(name)
                continue

            tool = registry.activate_tool(name)
            if tool is not None:
                activated.append(name)
                schema = tool.to_api_schema()
                lines.append(f"## {name}")
                lines.append(f"Description: {tool.description}")
                lines.append(f"Arguments: {json.dumps(schema.get('input_schema', {}))}")
                lines.append("")
            else:
                not_found.append(name)

        result_parts: list[str] = []

        if activated:
            result_parts.append(
                f"Activated {len(activated)} tool(s): {', '.join(activated)}. "
                f"They are now available for use.\n"
            )
            result_parts.extend(lines)

        if already_active:
            result_parts.append(
                f"Already active: {', '.join(already_active)}"
            )

        if not_found:
            # Search deferred for close matches to help the model.
            deferred = registry.get_deferred_tools()
            suggestions = ""
            if deferred:
                all_deferred = list(deferred.keys())
                candidates = sorted(
                    all_deferred,
                    key=lambda n: self._similarity(n, not_found[0]),
                    reverse=True,
                )[:5]
                if candidates:
                    suggestions = (
                        f"Available deferred tools (use select: to load): "
                        f"{', '.join(candidates)}"
                    )
            if suggestions:
                result_parts.append(f"Not found: {', '.join(not_found)}. {suggestions}")
            else:
                result_parts.append(
                    f"Not found: {', '.join(not_found)}. "
                    f"No deferred tools available."
                )

        if not result_parts:
            return "No tools were activated."

        return "\n".join(result_parts)

    def _handle_search(self, query: str, registry: ToolRegistry) -> str:
        """Search deferred tools and return ranked results."""
        results = registry.search_deferred(query)
        if not results:
            deferred = registry.get_deferred_tools()
            if not deferred:
                return "No deferred tools are available. All tools are already loaded."
            available = ", ".join(sorted(deferred.keys()))
            return (
                f"No tools match '{query}'.\n\n"
                f"Available deferred tools: {available}\n\n"
                f"Use 'select:Name' to load one or more of them."
            )

        lines = [f"Found {len(results)} tool(s) matching '{query}':\n"]
        for tool in results:
            hint = getattr(tool, "_search_hint", "") or ""
            hint_str = f" [hint: {hint}]" if hint else ""
            lines.append(f"- **{tool.name}**{hint_str}")
            # Truncate description to keep output compact.
            desc = tool.description.split("\n")[0][:120]
            lines.append(f"  {desc}")
        lines.append("")
        lines.append("Use 'select:Name' to load the tool(s) you need.")

        return "\n".join(lines)

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Simple prefix + character overlap similarity for suggestions."""
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        # Common prefix bonus.
        i = 0
        while i < min(len(a), len(b)) and a[i] == b[i]:
            i += 1
        prefix_score = i / max(len(a), len(b))
        # Character overlap.
        common = len(set(a) & set(b))
        overlap_score = common / max(len(set(a)), len(set(b)), 1)
        return prefix_score * 0.6 + overlap_score * 0.4

    # ── UI rendering ──────────────────────────────────────────────

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, ToolSearchInput)
        q = input.query[:60]
        return f"Tool search: {q}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"Tool search failed: {content[:100]}"
        return f"Tool search ({len(content)} chars)"
