"""Tool result budget management with disk persistence.

When a tool returns output exceeding max_chars, instead of hard-truncating
and losing data forever, the full output is persisted asynchronously to disk
and a reference block with preview is returned to the AI.

The AI can read the preview to decide whether to fetch the full output
via read_file on the persisted path.

Integration example in CoreExecutionEngine._query_loop():

    from xxcode.core.budget import apply_tool_result_budget

    # In the tool execution loop:
    result = await self._registry.execute(tc, context)
    truncated = await apply_tool_result_budget(
        raw_output=result.content,
        tool_use_id=tc.id,
        session_dir=self.config.session_dir,
        max_chars=self.config.max_tool_output_chars,
    )
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": tc.id,
        "content": truncated,
    })
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiofiles

logger = logging.getLogger(__name__)


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable KB/MB/GB."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _hard_truncate(content: str, max_chars: int) -> str:
    """Fallback: traditional head+tail truncation when persistence fails."""
    if len(content) <= max_chars:
        return content

    half = max_chars // 2
    head = content[:half]
    tail = content[-half:]

    removed = len(content) - max_chars
    return f"{head}\n\n... [{removed} characters truncated] ...\n\n{tail}"


async def apply_tool_result_budget(
    raw_output: str,
    tool_use_id: str,
    session_dir: Path,
    max_chars: int = 50000,
    preview_chars: int = 2000,
) -> str:
    """Apply tool result budget: persist to disk if oversized, return preview.

    Args:
        raw_output: The full raw output string from a tool call.
        tool_use_id: Unique ID of the tool call (used as filename).
        session_dir: Base session directory (e.g. ~/.xxcode/sessions).
        max_chars: Maximum characters allowed in the response to the AI.
        preview_chars: How many characters to include in the preview block.

    Returns:
        If raw_output fits within max_chars: the original string unchanged.
        Otherwise: a <persisted-output> XML block with the save path,
        size info, and a preview of the first N characters.

    The persisted file is saved to:
        {session_dir}/tool-results/{tool_use_id}.txt

    If persistence fails (disk full, permission denied, etc.), falls back
    to hard truncation with a warning message.
    """
    # If within budget, return as-is (no disk write needed)
    if len(raw_output) <= max_chars:
        return raw_output

    # Compute paths
    results_dir = session_dir / "tool-results"
    file_path = results_dir / f"{tool_use_id}.txt"

    # Calculate size
    size_bytes = len(raw_output.encode("utf-8", errors="replace"))
    size_label = _format_size(size_bytes)

    # Extract preview (first N chars)
    preview = raw_output[:preview_chars]
    if len(raw_output) > preview_chars:
        preview += "\n..."

    # Try to persist full output to disk
    try:
        os.makedirs(results_dir, exist_ok=True)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(raw_output)

        logger.debug(
            "Persisted tool output (%s) to %s", size_label, file_path
        )

    except (OSError, PermissionError, IOError) as e:
        logger.warning(
            "Failed to persist tool output to %s: %s. Falling back to truncation.",
            file_path, e,
        )
        # Degrade: hard truncation + warning
        truncated = _hard_truncate(raw_output, max_chars)
        return (
            truncated
            + f"\n\n... [Warning: Failed to persist full output to disk "
            f"due to error: {e}]"
        )

    # Build the persisted-output reference block
    output_block = (
        f"<persisted-output>\n"
        f"Output too large ({size_label}). "
        f"Full output saved to: {file_path}\n\n"
        f"Preview (first {min(preview_chars, len(raw_output))} characters):\n"
        f"{preview}\n"
        f"</persisted-output>"
    )

    return output_block


def clamp_to_absolute_max(content: str, absolute_max: int) -> str:
    """Hard-truncate content exceeding the absolute per-result ceiling.

    Unlike chunked persistence, this is a safety backstop — once content
    exceeds this limit (default 400KB / ~100K tokens), we forcibly truncate
    to prevent runaway allocations from crashing the process.

    Returns the original content if it fits; otherwise returns a head+tail
    snippet with a clear truncation notice.
    """
    if len(content) <= absolute_max:
        return content

    half = absolute_max // 2
    head = content[:half]
    tail = content[-half:]
    removed = len(content) - absolute_max

    return (
        f"{head}\n\n"
        f"[OUTPUT TRUNCATED: {_format_size(len(content.encode('utf-8', errors='replace')))}, "
        f"{removed} characters removed — exceeds absolute limit of "
        f"{_format_size(absolute_max)}]\n\n"
        f"{tail}"
    )


async def apply_aggregate_result_budget(
    results: list[dict[str, Any]],
    max_total_chars: int,
) -> list[dict[str, Any]]:
    """Enforce aggregate per-message limit across concurrent tool results.

    When the total content across all results exceeds max_total_chars, the
    largest results are progressively truncated until the total fits within
    the budget. Each truncated result is replaced with a preview block
    plus a reference telling the model to use read_file on the persisted file.

    The original ``results`` list is NOT modified — a new list is returned.

    Args:
        results: List of tool_result dicts with "content" and "tool_use_id" keys.
        max_total_chars: Maximum total characters allowed across all results.

    Returns:
        A new list of result dicts with content potentially truncated.
    """
    total = sum(len(r.get("content", "")) for r in results)
    if total <= max_total_chars:
        return results

    # Sort by content size descending — truncate largest first.
    indexed = [(i, r) for i, r in enumerate(results)]
    indexed.sort(key=lambda x: len(x[1].get("content", "")), reverse=True)

    # Work with a mutable copy of the results.
    output = [dict(r) for r in results]

    for idx, _ in indexed:
        if total <= max_total_chars:
            break
        content = output[idx].get("content", "")
        if not content:
            continue
        budget_for_this = max(0, max_total_chars - (total - len(content)))
        output[idx]["content"] = _truncate_aggregate_content(
            content,
            budget_for_this,
            result_count=len(results),
        )
        total = sum(len(r.get("content", "")) for r in output)

    return output


def _truncate_aggregate_content(content: str, budget: int, result_count: int) -> str:
    """Return content that fits the aggregate budget, including any notice."""
    if budget <= 0:
        return ""
    if len(content) <= budget:
        return content

    notice = (
        f"\n\n... [Aggregate budget: {len(content) - budget} characters truncated "
        f"across {result_count} concurrent results. "
        "If a <persisted-output> path appears above, use read_file to retrieve the full content.]"
    )
    if len(notice) >= budget:
        return content[:budget]

    return content[: budget - len(notice)] + notice


__all__ = ["apply_tool_result_budget", "clamp_to_absolute_max", "apply_aggregate_result_budget"]
