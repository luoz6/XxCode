"""Helpers for injecting hidden context into agent message history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..memory.injection import (
    build_memory_index_message,
    build_recalled_memories_message,
)
from ..memory.recall import MemoryRecall
from .state import AgentState


def _inject_recalled_memories(state: AgentState, recalled: list[MemoryRecall]) -> None:
    """Inject recalled memory content before the current user turn."""
    message = build_recalled_memories_message(recalled)
    if message is None:
        return

    _insert_before_current_user_message(state, message)


def _inject_memory_index_context(state: AgentState, memory_dir: str) -> None:
    """Inject the current MEMORY.md index as hidden user context."""
    message = build_memory_index_message(Path(memory_dir))
    if message is not None:
        _insert_before_current_user_message(state, message)


def _strip_message_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal message-only metadata before sending requests to the API."""
    api_messages: list[dict[str, Any]] = []
    for msg in messages:
        cleaned = {
            key: value
            for key, value in msg.items()
            if key not in {"metadata", "isMeta"}
        }
        api_messages.append(cleaned)
    return api_messages


def _is_system_hint_block(block: dict[str, Any]) -> bool:
    if block.get("type") != "text":
        return False
    text = block.get("text", "")
    return isinstance(text, str) and "<system_hint>" in text


def _is_tool_result_carrier_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False

    content = message.get("content", [])
    if not isinstance(content, list):
        return False

    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            return False
        if block.get("type") == "tool_result":
            saw_tool_result = True
            continue
        if _is_system_hint_block(block):
            continue
        return False

    return saw_tool_result


def _insert_before_current_user_message(
    state: AgentState,
    message: dict[str, Any],
) -> None:
    """Insert metadata before the current natural-language user query."""
    target_idx: int | None = None
    for idx in range(len(state.messages) - 1, -1, -1):
        candidate = state.messages[idx]
        if candidate.get("role") != "user":
            continue
        if _is_tool_result_carrier_message(candidate):
            continue
        target_idx = idx
        break

    if target_idx is None:
        state.messages.append(message)
        return

    state.messages.insert(target_idx, message)
