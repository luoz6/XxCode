"""EditFileTool UI rendering — diff-view, error diagnostics, grouped display.

Decoupled from core execution logic (tool.py). All functions are pure:
they take inputs and return strings, with no side effects.

Three rendering tiers:
  1. render_tool_use       — single-edit call description with unified diff preview
  2. render_grouped_tool_use — batch summary for multiple edits on the same file
  3. render_tool_result     — result rendering: unified diff on success,
                               context-mismatch diagnostics on failure
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from .types import EditErrorCode


# ═════════════════════════════════════════════════════════════════════
# Internal helpers
# ═════════════════════════════════════════════════════════════════════

def _truncate_line(line: str, max_len: int = 100) -> str:
    """Truncate a single line for display."""
    if len(line) <= max_len:
        return line
    return line[:max_len - 3] + "..."

def _count_lines(text: str) -> int:
    """Count lines in a string."""
    return max(1, text.count("\n") + 1)


# ═════════════════════════════════════════════════════════════════════
# 1. Single-edit render — unified diff preview
# ═════════════════════════════════════════════════════════════════════

def render_tool_use(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Build a human-readable description of an edit_file call.

    Uses difflib.unified_diff for proper hunk-based rendering with
    @@ -L,N +L,N @@ context headers. Limits output to 12 diff lines
    for readability.
    """
    file_name = Path(file_path).name
    mode_label = " (all)" if replace_all else ""

    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{file_name}",
            tofile=f"b/{file_name}",
            n=2,
        )
    )

    header = f"edit_file{mode_label}: {file_name}"

    if not diff_lines:
        # Fallback: line-by-line for simple changes
        if old_lines and new_lines:
            parts = [header]
            parts.append(f"  [-]{_truncate_line(old_lines[0].rstrip())}")
            parts.append(f"  [+]{_truncate_line(new_lines[0].rstrip())}")
            return "\n".join(parts)
        return header

    # Limit to 12 diff lines
    if len(diff_lines) > 14:  # 2 header + 12 content
        diff_lines = diff_lines[:14]
        diff_lines.append("  ... (diff truncated)\n")

    return header + "\n" + "".join(diff_lines).rstrip()


# ═════════════════════════════════════════════════════════════════════
# 2. Grouped render — batch summary
# ═════════════════════════════════════════════════════════════════════

def render_grouped_tool_use(inputs: list[Any]) -> str:
    """Build a compact batch summary for multiple edits.

    Groups edits by file when they target the same path.
    """
    if len(inputs) == 1:
        inp = inputs[0]
        return render_tool_use(
            inp.file_path, inp.old_string, inp.new_string,
            getattr(inp, "replace_all", False),
        )

    # Group by file path
    by_file: dict[str, list[Any]] = {}
    for inp in inputs:
        fp = inp.file_path
        by_file.setdefault(fp, []).append(inp)

    parts: list[str] = []
    for fp, edits in by_file.items():
        file_name = Path(fp).name
        if len(edits) == 1:
            inp = edits[0]
            parts.append(
                render_tool_use(
                    inp.file_path, inp.old_string, inp.new_string,
                    getattr(inp, "replace_all", False),
                )
            )
        else:
            # Multi-edit summary per file
            total_old_lines = sum(_count_lines(e.old_string) for e in edits)
            total_new_lines = sum(_count_lines(e.new_string) for e in edits)
            parts.append(
                f"edit_file: {file_name} ({len(edits)} edits, "
                f"[-]{total_old_lines} lines, [+]{total_new_lines} lines)"
            )

    return "\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
# 3. Result render — diff stats (success) / error diagnostics (failure)
# ═════════════════════════════════════════════════════════════════════

def render_tool_result(content: str, is_error: bool) -> str:
    """Build a human-readable summary of an edit_file result.

    Success: shows replacement count and unified diff if embedded.
    Failure: renders context-mismatch diagnostics when old_string
             wasn't found in the file.
    """
    if is_error:
        return _render_edit_error(content)

    # If the result includes an embedded diff (separated by \n---DIFF---\n),
    # extract and display it compactly.
    if "\n---DIFF---\n" in content:
        summary, diff_text = content.split("\n---DIFF---\n", 1)
        if diff_text.strip():
            return f"{summary}\n{diff_text.strip()}"
        return summary

    if "occurrence(s) replaced" in content:
        return content
    return f"Edit: {content[:120]}"


# Human-readable labels for each error code.
_ERROR_CODE_LABELS: dict[EditErrorCode, str] = {
    EditErrorCode.NO_OP: "old_string and new_string are identical",
    EditErrorCode.PERMISSION_DENIED: "permission denied",
    EditErrorCode.EMPTY_OLD_ON_EXISTING: "empty old_string on existing file",
    EditErrorCode.FILE_NOT_FOUND: "file not found",
    EditErrorCode.NOTEBOOK_REDIRECT: "use notebook_edit for .ipynb files",
    EditErrorCode.UNREAD_FILE: "file not yet read",
    EditErrorCode.STALE_READ: "file modified since last read",
    EditErrorCode.STRING_NOT_FOUND: "string to replace not found in file",
    EditErrorCode.MULTIPLE_MATCHES: "multiple matches found",
    EditErrorCode.FILE_TOO_LARGE: "file exceeds size limit",
    EditErrorCode.WRITE_FAILED: "file write error",
    EditErrorCode.READ_FAILED: "file read error",
    EditErrorCode.CASCADING_EDIT: "cascading edit detected",
}


def _parse_error_code(content: str) -> EditErrorCode | None:
    """Extract EditErrorCode from a structured [ErrCode N] error message."""
    m = re.search(r"\[ErrCode (\d+)\]", content)
    if m:
        try:
            return EditErrorCode(int(m.group(1)))
        except ValueError:
            pass
    return None


def _render_edit_error(content: str) -> str:
    """Render edit failure with diagnostics.

    Parses structured [ErrCode N] errors and maps them to concise
    human-readable messages. For actionable errors, includes guidance
    to help the model self-correct.
    """
    code = _parse_error_code(content)

    if code is None:
        return f"Edit failed: {content[:200]}"

    label = _ERROR_CODE_LABELS.get(code, code.name.replace("_", " ").lower())

    if code == EditErrorCode.STRING_NOT_FOUND:
        return (
            f"Edit failed ({label}).\n"
            "  The file may have changed since it was last read.\n"
            "  Tip: re-read the file to see current content, then retry the edit."
        )
    if code == EditErrorCode.NOTEBOOK_REDIRECT:
        return (
            f"Edit blocked ({label}).\n"
            "  Use the notebook_edit tool to modify .ipynb files.\n"
            "  If notebook_edit is not in your tool list, use tool_search\n"
            "  with 'select:notebook_edit' to load it first.\n"
            "  edit_file only works with plain-text files."
        )
    if code == EditErrorCode.CASCADING_EDIT:
        return (
            f"Edit failed ({label}).\n"
            "  A previous edit on this file inserted text that matches\n"
            "  old_string. Re-read the file and adjust the edit to target\n"
            "  the original content."
        )
    if code == EditErrorCode.MULTIPLE_MATCHES:
        return (
            f"Edit failed ({label}).\n"
            "  Include more surrounding context to make the match unique,\n"
            "  or set replace_all=true to replace all occurrences."
        )
    if code == EditErrorCode.NO_OP:
        return f"Edit skipped ({label})."

    return f"Edit failed ({label})."


# ═════════════════════════════════════════════════════════════════════
# 4. Error context renderer — detailed mismatch diagnostics
# ═════════════════════════════════════════════════════════════════════

def render_error_context(
    file_path: str,
    old_string: str,
    file_content: str | None = None,
) -> str:
    """Diagnose WHY old_string wasn't matched — renders context diff.

    Call this separately (e.g., from a diagnostic /slash command or
    enhanced error handling) to get rich mismatch details.

    Args:
        file_path: Absolute path to the target file.
        old_string: The search string that failed to match.
        file_content: Current file contents (if known). If None,
                      attempts to read the file on disk.

    Returns:
        Diagnostic string showing closest-matching context.
    """
    if file_content is None:
        try:
            file_content = Path(file_path).read_text(encoding="utf-8")
        except Exception:
            return f"Cannot read {file_path} for diagnostics."

    old_first_line = old_string.split("\n")[0].strip()

    if not old_first_line:
        return "Error: old_string appears to be empty or whitespace-only."

    # Search for the closest match — fuzzy line-by-line scan.
    file_lines = file_content.split("\n")
    best_idx = -1
    best_score = 0

    for i, line in enumerate(file_lines):
        score = _line_similarity(old_first_line, line.strip())
        if score > best_score:
            best_score = score
            best_idx = i

    if best_score < 0.3:
        return (
            f"No similar lines found in {Path(file_path).name}.\n"
            f"  Searched for: '{_truncate_line(old_first_line)}'\n"
            f"  The file may have been completely rewritten."
        )

    # Show context around the best match
    ctx_start = max(0, best_idx - 3)
    ctx_end = min(len(file_lines), best_idx + 4)

    lines: list[str] = [
        f"Closest match at line {best_idx + 1} ({best_score:.0%} similarity):",
        "",
    ]

    for i in range(ctx_start, ctx_end):
        marker = ">>>" if i == best_idx else "   "
        line_text = _truncate_line(file_lines[i], 120)
        lines.append(f"  {marker} {i + 1:4d}  {line_text}")

    lines.append("")
    lines.append(f"  Expected: [-]{_truncate_line(old_first_line)}")

    return "\n".join(lines)


def _line_similarity(a: str, b: str) -> float:
    """Simple token-based line similarity for fuzzy matching."""
    if a == b:
        return 1.0

    a_tokens = set(a.split())
    b_tokens = set(b.split())

    if not a_tokens:
        return 0.0

    intersection = a_tokens & b_tokens
    return len(intersection) / max(len(a_tokens), len(b_tokens))
