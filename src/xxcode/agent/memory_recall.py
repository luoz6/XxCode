"""Shared memory recall orchestration helpers for agent loops."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Config
from ..memory.agent_memory import recall_agent_memories_for_query
from ..memory.injection import build_recalled_memories_message, recalled_memory_ids
from ..memory.recall import MemoryRecall, recall_memories_for_query
from .recall_utils import (
    clip_recall_text,
    format_tool_input_for_recall,
    get_recent_tool_names,
    is_read_like_tool,
    should_trigger_followup_recall,
)
from .state import AgentState

logger = logging.getLogger(__name__)


def _normalize_client_factory(
    build_client: Callable[[], Any],
) -> Callable[[], Awaitable[Any]]:
    """Adapt sync, coroutine, and general-awaitable factories to one async shape."""

    async def _async_factory() -> Any:
        result = build_client()
        if inspect.isawaitable(result):
            return await result
        return result

    return _async_factory


def collect_tool_observations(
    executor: Any,
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Capture the latest tool outcomes for same-turn follow-up recall."""
    observations: list[dict[str, Any]] = []
    for result in tool_results:
        if result.get("type") != "tool_result":
            continue
        tid = result.get("tool_use_id", "")
        if not tid:
            continue
        slot = executor.get_slot(tid)
        if slot is None:
            continue
        observations.append(
            {
                "call": slot.tc,
                "tool": executor._registry.get(slot.tc.name),
                "is_error": slot.is_error,
                "content": slot.truncated if slot.truncated else result.get("content", ""),
            }
        )
    return observations


async def run_memory_recall_with_query(
    *,
    config: Config,
    state: AgentState,
    query: str,
    build_client: Callable[[], Any],
    recent_tools: list[str] | None = None,
    already_surfaced: set[str] | None = None,
) -> list[MemoryRecall]:
    """Run memory recall for a specific query within the current session."""
    try:
        memory_dir = Path(config.auto_memory_directory)
        if not memory_dir.exists() or not query:
            return []

        if recent_tools is None:
            recent_tools = get_recent_tool_names(state.messages)
        if already_surfaced is None:
            already_surfaced = recalled_memory_ids(state.messages)

        client_factory = _normalize_client_factory(build_client)

        main_results, agent_results = await asyncio.gather(
            recall_memories_for_query(
                query=query,
                memory_dir=memory_dir,
                client_factory=client_factory,
                recent_tools=recent_tools,
                already_surfaced=already_surfaced,
            ),
            recall_agent_memories_for_query(
                agent_type="main",
                project_root=config.cwd,
                query=query,
                client_factory=client_factory,
                recent_tools=recent_tools,
                already_surfaced=already_surfaced,
            ),
        )
        return list(main_results) + list(agent_results)
    except Exception:
        logger.debug("Memory recall pipeline failed", exc_info=True)
        return []


async def append_fresh_recalled_memories(
    *,
    config: Config,
    state: AgentState,
    tool_observations: list[dict[str, Any]],
    build_client: Callable[[], Any],
) -> None:
    """Append newly recalled memories after tool results in the same user turn."""
    if not config.auto_memory_enabled or not config.auto_memory_directory:
        return
    if not state.last_query:
        return
    if not should_trigger_followup_recall(tool_observations):
        return

    recall_query = build_followup_recall_query(state.last_query, tool_observations)
    if not recall_query:
        return

    recalled = await run_memory_recall_with_query(
        config=config,
        state=state,
        query=recall_query,
        build_client=build_client,
        recent_tools=get_recent_tool_names(state.messages),
        already_surfaced=recalled_memory_ids(state.messages),
    )
    if not recalled:
        return

    message = build_recalled_memories_message(recalled)
    if message is not None:
        state.messages.append(message)


def build_followup_recall_query(
    task_query: str,
    tool_observations: list[dict[str, Any]],
) -> str:
    """Build a compact recall query from the task and latest tool outcomes."""
    errors: list[str] = []
    observations: list[str] = []

    for observation in tool_observations:
        call = observation["call"]
        details = format_tool_input_for_recall(call.input)
        label = call.name if not details else f"{call.name} ({details})"
        content = clip_recall_text(observation.get("content", ""))
        line = f"- {label}: {content}"
        if observation.get("is_error"):
            errors.append(line)
        elif is_read_like_tool(call.name, observation.get("tool"), call.input):
            observations.append(line)

    parts = [f"Task: {task_query}"]
    if errors:
        parts.extend(["", "Recent tool errors:", *errors[:3]])
    elif observations:
        parts.extend(["", "Recent observations:", *observations[:3]])

    return "\n".join(parts)


__all__ = [
    "append_fresh_recalled_memories",
    "build_followup_recall_query",
    "collect_tool_observations",
    "run_memory_recall_with_query",
]
