"""L2: Microcompact — dual-path compression of stale tool results.

Path A (cache cold):  In-place content replacement — old tool_result
    content is cleared locally because the prefix cache will miss anyway.

Path B (cache warm):  Cache edits — the messages list is preserved
    verbatim; instead a list of CacheEdit instructions tells the caller
    which tool_use_ids to evict server-side via the API's cache layer.
"""

import copy
from dataclasses import dataclass
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────

# Only results from these tools are eligible for microcompact.
_COMPRESSIBLE_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "run_shell",
    "grep_search",
    "glob_match",
    "edit_file",
    "write_file",
})

# Placeholder written into content when is_cache_cold=True.
_CLEARED_PLACEHOLDER = "[Old tool result content cleared]"

# ── Data classes ──────────────────────────────────────────────────────


@dataclass
class CacheEdit:
    """Instruction to evict a tool_result from the server-side cache."""
    tool_use_id: str
    action: str = "delete"


# ── Helpers ───────────────────────────────────────────────────────────


def _build_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Scan assistant messages for tool_use blocks → {tool_use_id: tool_name}.

    tool_result blocks carry tool_use_id but NOT tool_name (per API schema).
    The name lives in the preceding assistant tool_use block.
    """
    name_map: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "")
                if tool_id and tool_name:
                    name_map[tool_id] = tool_name
    return name_map


def _iter_tool_result_blocks(
    messages: list[dict[str, Any]],
) -> list[tuple[int, int, dict[str, Any]]]:
    """Walk messages and yield (msg_idx, block_idx, block) for every tool_result.

    Returns a flat list so we can scan backwards for keep_recent logic.
    """
    result: list[tuple[int, int, dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if block.get("type") == "tool_result":
                result.append((i, j, block))
    return result


# ── Main entry point ──────────────────────────────────────────────────


def microcompact_messages(
    messages: list[dict[str, Any]],
    is_cache_cold: bool,
    keep_recent: int = 1,
) -> tuple[list[dict[str, Any]], list[CacheEdit]]:
    """Compress stale tool results from compressible tools.

    Args:
        messages: The full message history.
        is_cache_cold: True → Path A (replace content locally).
                       False → Path B (emit CacheEdit instructions).
        keep_recent: How many of the most recent compressible tool
                     results to preserve verbatim.

    Returns:
        (messages, edits) where *messages* is always a deep copy and
        *edits* is non-empty only on Path B.
    """
    # Always deep-copy — never mutate the caller's list.
    result: list[dict[str, Any]] = copy.deepcopy(messages)
    edits: list[CacheEdit] = []
    keep_recent = max(1, keep_recent)

    # Build tool_use_id → tool_name map from assistant tool_use blocks.
    # tool_result blocks carry the ID but NOT the name (per API schema).
    tool_name_map = _build_tool_name_map(result)

    # Collect every tool_result block.
    all_blocks = _iter_tool_result_blocks(result)

    # Partition into compressible vs non-compressible.
    compressible: list[tuple[int, int, dict[str, Any]]] = []
    for msg_idx, block_idx, block in all_blocks:
        tool_use_id = block.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_use_id, "")
        if tool_name in _COMPRESSIBLE_TOOLS:
            compressible.append((msg_idx, block_idx, block))

    if not compressible:
        return result, edits

    # The most recent *keep_recent* entries are fresh — skip them.
    stale_count = max(0, len(compressible) - keep_recent)
    stale_blocks = compressible[:stale_count]

    if is_cache_cold:
        # Path A: mutate content directly in the deep copy.
        for msg_idx, block_idx, _block in stale_blocks:
            result[msg_idx]["content"][block_idx]["content"] = _CLEARED_PLACEHOLDER
    else:
        # Path B: keep content intact, issue cache-delete instructions.
        for _msg_idx, _block_idx, block in stale_blocks:
            tool_use_id = block.get("tool_use_id", "")
            if tool_use_id:
                edits.append(CacheEdit(tool_use_id=tool_use_id))

    return result, edits


def count_cleared_tool_results(messages: list[dict[str, Any]]) -> int:
    """Count tool_result blocks replaced with the microcompact placeholder."""
    count = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                block.get("type") == "tool_result"
                and block.get("content") == _CLEARED_PLACEHOLDER
            ):
                count += 1
    return count


# ── Legacy helpers (kept for external callers) ────────────────────────

_DEFAULT_MAX_RESULT_CHARS = 20_000
_DEFAULT_MAX_TOTAL_CHARS = 100_000


def microcompact_result(content: str, max_chars: int = _DEFAULT_MAX_RESULT_CHARS) -> str:
    """Truncate a single result to max_chars, keeping head 50% + tail 50%."""
    if len(content) <= max_chars:
        return content

    half = max_chars // 2
    head = content[:half]
    tail = content[-half:]

    removed = len(content) - max_chars
    return f"{head}\n\n... [{removed} characters truncated] ...\n\n{tail}"
