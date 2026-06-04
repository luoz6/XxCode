"""Agent type definitions — tool allowlists, models, and metadata for sub-agents.

Each definition describes what a sub-agent type can do, what tools it has
access to, and which model it prefers.  The registry in AgentTool picks the
right definition based on ``subagent_type``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentDef:
    """Static definition of a sub-agent type.

    Attributes:
        name:         Unique identifier (e.g. "Explore", "Plan").
        description:  Human-readable summary shown to the model.
        tools_allowlist:  If set, only these tool names are available.
                          If None, all registered tools are available.
        tools_denylist:   Tool names to exclude (applied after allowlist).
        model:         Model override — None means "inherit from parent".
        is_read_only:  If True, only read-only tools are available.
        max_turns:     Hard turn limit to prevent runaway loops.
    """

    name: str
    description: str
    tools_allowlist: set[str] | None = None
    tools_denylist: set[str] | None = None
    model: str | None = None
    is_read_only: bool = False
    max_turns: int = 50
    permission_mode: str = "inherit"
    isolation: str | None = None  # None = shared fs, "worktree" = git worktree


# ── Built-in agent type definitions ──────────────────────────────────

AGENT_DEFINITIONS: dict[str, AgentDef] = {
    "general-purpose": AgentDef(
        name="general-purpose",
        description=(
            "General-purpose agent for researching complex questions, "
            "searching for code, and executing multi-step tasks."
        ),
        tools_allowlist=None,
        tools_denylist=None,
        model=None,
        is_read_only=False,
        max_turns=50,
        permission_mode="inherit",
    ),
    "Explore": AgentDef(
        name="Explore",
        description=(
            "Fast read-only search agent for locating code. "
            "Use it to find files by pattern, grep for symbols or keywords, "
            "or answer 'where is X defined / which files reference Y'."
        ),
        tools_allowlist={"read_file", "grep_search", "glob_match"},
        tools_denylist=None,
        model=None,
        is_read_only=True,
        max_turns=30,
        permission_mode="bypass",
    ),
    "Plan": AgentDef(
        name="Plan",
        description=(
            "Software architect agent for designing implementation plans. "
            "Use this when you need to plan the implementation strategy "
            "for a task. Returns step-by-step plans and identifies "
            "critical files."
        ),
        tools_allowlist={"read_file", "grep_search", "glob_match"},
        tools_denylist=None,
        model=None,
        is_read_only=True,
        max_turns=40,
        permission_mode="bypass",
    ),
    "Coordinator": AgentDef(
        name="Coordinator",
        description=(
            "Coordinator agent for orchestrating background workers. "
            "Always use run_in_background=true when spawning workers. "
            "Never use sync mode. "
            "Use TaskWait to wait for workers. "
            "Never busy-poll with repeated TaskList calls. "
            "Use TaskList and TaskGet for inspection and result retrieval only. "
            "After all target workers settle, synthesize their results into one final answer."
        ),
        tools_allowlist={
            "Agent",
            "TaskList",
            "TaskGet",
            "TaskWait",
            "TaskStop",
            "SendMessage",
        },
        tools_denylist=None,
        model=None,
        is_read_only=False,
        max_turns=100,
        permission_mode="bypass",
        isolation="worktree",
    ),
    "claude-code-guide": AgentDef(
        name="claude-code-guide",
        description=(
            "Use this agent when the user asks questions about Claude Code "
            "features, hooks, slash commands, MCP servers, settings, IDE "
            "integrations, or the Claude Agent SDK."
        ),
        tools_allowlist={"read_file", "grep_search", "glob_match"},
        tools_denylist=None,
        model=None,
        is_read_only=True,
        max_turns=20,
        permission_mode="inherit",
    ),
}


def get_agent_definition(subagent_type: str) -> AgentDef:
    """Resolve an agent type name to its definition.

    Falls back to ``general-purpose`` when the requested type is unknown.
    """
    if subagent_type in AGENT_DEFINITIONS:
        return AGENT_DEFINITIONS[subagent_type]

    logger.warning(
        "Unknown subagent_type '%s' — falling back to general-purpose.",
        subagent_type,
    )
    return AGENT_DEFINITIONS["general-purpose"]


def build_filtered_registry(
    base_registry,
    definition: AgentDef,
) -> Any:
    """Build a filtered ToolRegistry based on the agent definition.

    Returns a *new* ToolRegistry containing only the tools allowed by
    the definition's allowlist / denylist / read_only_only filters.
    The original registry is unchanged.
    """
    from ..tools.registry import ToolRegistry

    filtered = ToolRegistry()

    for tool in base_registry.list_tools():
        # Apply allowlist filter.
        if definition.tools_allowlist is not None:
            if tool.name not in definition.tools_allowlist:
                continue

        # Apply denylist filter.
        if definition.tools_denylist is not None:
            if tool.name in definition.tools_denylist:
                continue

        # Apply read-only-only filter.
        if definition.is_read_only:
            if not tool.is_read_only():
                continue

        filtered.register(tool)

    return filtered
