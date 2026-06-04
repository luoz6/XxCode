"""Unified diff generation for edit_file tool results.

Uses Python's difflib to produce compact, human-readable diffs
for both single-edit previews and batch result summaries.
"""

from __future__ import annotations

import difflib


def generate_diff(
    file_path: str,
    old_content: str,
    new_content: str,
    context_lines: int = 3,
) -> str:
    """Generate a unified diff between old and new content.

    Returns empty string when contents are identical.
    Limits output to 30 diff lines for readability.
    """
    if old_content == new_content:
        return ""

    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=context_lines,
        )
    )

    if not diff_lines:
        return ""

    if len(diff_lines) > 34:  # header (2) + 30 diff lines + optional
        diff_lines = diff_lines[:34]
        diff_lines.append("  ... (diff truncated)\n")

    return "".join(diff_lines).rstrip("\n")


def compute_edit_diff_stat(
    old_content: str,
    new_content: str,
) -> tuple[int, int]:
    """Return (lines_removed, lines_added) for a compact change summary."""
    old_count = old_content.count("\n") + (0 if old_content.endswith("\n") else 1) if old_content else 0
    new_count = new_content.count("\n") + (0 if new_content.endswith("\n") else 1) if new_content else 0
    removed = max(0, old_count - new_count) if old_count != new_count else old_count
    added = max(0, new_count - old_count) if old_count != new_count else new_count
    # When both non-zero and line counts differ, use actual change direction
    if old_count != new_count:
        removed = max(0, old_count - new_count)
        added = max(0, new_count - old_count)
    return removed, added
