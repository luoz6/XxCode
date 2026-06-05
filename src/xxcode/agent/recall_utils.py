"""Shared helpers for memory recall query construction."""

from __future__ import annotations

from typing import Any

_READ_LIKE_TOOL_NAMES = frozenset({
    "read_file",
    "grep_search",
    "glob_match",
})


def get_recent_tool_names(messages: list[dict[str, Any]], *, max_turns: int = 10) -> list[str]:
    """Extract recently used tool names from recent assistant turns."""
    names: list[str] = []
    for msg in reversed(messages[-max_turns:]):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if name and name not in names:
                names.append(name)
    return names


def is_read_like_tool(
    name: str,
    tool: Any,
    raw_input: dict[str, Any] | None = None,
    *,
    read_like_names: set[str] | frozenset[str] | None = None,
) -> bool:
    """Return whether a tool should count as a read-like memory recall signal."""
    raw_input = raw_input or {}
    has_location_hint = any(
        isinstance(raw_input.get(key), str) and raw_input.get(key, "").strip()
        for key in ("file_path", "path", "pattern", "query")
    )
    if not has_location_hint:
        return False

    if name in (read_like_names or _READ_LIKE_TOOL_NAMES):
        return True

    if tool is None:
        return False

    try:
        validated_input = tool.input_schema.model_validate(raw_input)
    except Exception:
        validated_input = None

    try:
        return bool(tool.is_read_only(validated_input))
    except TypeError:
        return bool(tool.is_read_only())
    except Exception:
        return False


def format_tool_input_for_recall(raw_input: dict[str, Any]) -> str:
    """Extract compact location hints from a tool call input."""
    if not isinstance(raw_input, dict):
        return ""

    hints: list[str] = []
    for key in ("file_path", "path", "pattern", "query", "command"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            hints.append(f"{key}={value.strip()}")
        if len(hints) == 2:
            break
    return ", ".join(hints)


def clip_recall_text(text: str, *, limit: int = 400) -> str:
    """Condense recall content to a stable short preview."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 3, 1)] + "..."


def should_trigger_followup_recall(tool_observations: list[dict[str, Any]]) -> bool:
    """Trigger follow-up recall after read observations or tool failures."""
    if not tool_observations:
        return False

    for observation in tool_observations:
        if observation.get("is_error"):
            return True

    return any(
        is_read_like_tool(
            observation["call"].name,
            observation.get("tool"),
            observation["call"].input,
        )
        for observation in tool_observations
    )


__all__ = [
    "_READ_LIKE_TOOL_NAMES",
    "clip_recall_text",
    "format_tool_input_for_recall",
    "get_recent_tool_names",
    "is_read_like_tool",
    "should_trigger_followup_recall",
]
