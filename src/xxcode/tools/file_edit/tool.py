"""EditFileTool — exact string replacement with uniqueness constraint.

Implements editing primitives from Claude Code §10:
  P0: trailing whitespace stripping, quote normalization, structured error codes
  P1: read-before-edit enforcement, external-modification detection,
       cascading-edit protection, line-ending preservation
"""

from __future__ import annotations

import os
import sys as _sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import Tool
from .types import EditErrorCode, EditFileInput, FileStateEntry, _format_error, detect_line_endings
from .diff import generate_diff
from .ui import (
    render_grouped_tool_use as _render_grouped,
    render_tool_result as _render_result,
    render_tool_use as _render_use,
)
from ..path_utils import check_allowed_write_roots, resolve_tool_path


# ═════════════════════════════════════════════════════════════════════
# Quote normalization — curly → straight conversion
# ═════════════════════════════════════════════════════════════════════

# Curly (typographic) quote characters and their straight equivalents.
# Code copied from web pages, Word, or Google Docs often contains
# these; models output straight quotes.  Without normalization the
# match would fail silently on a file that looks correct to the eye.
_CURLY_TO_STRAIGHT: dict[int, str] = {
    0x201C: '"',   # " left double curly
    0x201D: '"',   # " right double curly
    0x2018: "'",   # ' left single curly
    0x2019: "'",   # ' right single curly
}


def _normalize_quotes(text: str) -> str:
    """Replace curly (typographic) quotes with straight ASCII equivalents."""
    return text.translate(_CURLY_TO_STRAIGHT)


# ── Preprocessing: new_string cleanup ────────────────────────────────

# File extensions where trailing whitespace is semantically meaningful.
# Markdown uses two trailing spaces for hard line breaks (<br> / \),
# so stripping would silently change document semantics.
_PRESERVE_TRAILING_WHITESPACE_EXTS: tuple[str, ...] = (".md", ".mdx")


def _strip_trailing_whitespace(text: str, file_path: str) -> str:
    """Strip trailing whitespace from each line, skipping Markdown files.

    LLMs frequently add stray trailing spaces/tabs at line ends.
    Stripping them before writing prevents the file from accumulating
    invisible whitespace that breaks subsequent edits (old_string
    won't match because the model doesn't "see" the trailing spaces
    it emitted last time).
    """
    ext = Path(file_path).suffix.lower()
    if ext in _PRESERVE_TRAILING_WHITESPACE_EXTS:
        return text
    lines = text.split("\n")
    stripped = [line.rstrip() for line in lines]
    return "\n".join(stripped)


# ── Two-stage matching ───────────────────────────────────────────────


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """Find search_string in file_content with progressive tolerance.

    Stage 1: exact match (fast path, most common).
    Stage 2: quote-normalized match (handles curly-quote drift).

    Returns the *original* substring from file_content on match,
    so callers preserve the file's quote style.  Returns None if
    neither stage matches.
    """
    # Stage 1: exact match.
    if search_string in file_content:
        return search_string

    # Stage 2: normalize curly quotes in both strings, retry.
    normalized_search = _normalize_quotes(search_string)
    normalized_file = _normalize_quotes(file_content)

    idx = normalized_file.find(normalized_search)
    if idx != -1:
        # Return the original (possibly curly-quoted) substring
        # from the file so we use the file's actual formatting.
        return file_content[idx:idx + len(search_string)]

    return None


def _count_occurrences(file_content: str, search_string: str) -> int:
    """Count occurrences using two-stage matching.

    Always counts via quote-normalized comparison so that mixed
    curly/straight quote files produce accurate occurrence counts.
    """
    actual = _find_actual_string(file_content, search_string)
    if actual is None:
        return 0
    nf = _normalize_quotes(file_content)
    ns = _normalize_quotes(search_string)
    return nf.count(ns)


# ═════════════════════════════════════════════════════════════════════
# EditFileTool
# ═════════════════════════════════════════════════════════════════════


class EditFileTool(Tool):
    """Perform exact string replacement in an existing file.

    Safety constraints:
      - old_string and new_string must differ
      - By default, old_string must appear exactly once (uniqueness)
      - replace_all=True bypasses the uniqueness check
    """

    name = "edit_file"
    description = (
        "Perform exact string replacement in an existing file. "
        "By default, old_string must appear exactly once in the file. "
        "Use replace_all=true to substitute every occurrence."
    )
    input_schema = EditFileInput

    _is_read_only = False
    _is_destructive = False

    def confirms_file_paths(self) -> bool:
        """On grant, the file path is added to the confirmed-paths whitelist."""
        return True

    # ── Rendering (delegates to ui.py) ────────────────────────────

    def render_tool_use(self, input: BaseModel) -> str:
        """Render a single edit_file call with inline diff preview."""
        assert isinstance(input, EditFileInput)
        return _render_use(
            input.file_path, input.old_string, input.new_string, input.replace_all,
        )

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """Summarize an edit_file result — delegates to ui.py."""
        return _render_result(content, is_error)

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch-render multiple edits grouped by target file."""
        return _render_grouped(inputs)

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve relative paths to absolute using cwd context."""
        assert isinstance(input, EditFileInput)
        fp = Path(input.file_path)
        if not fp.is_absolute():
            cwd = context.get("cwd", str(Path.cwd()))
            fp = Path(cwd) / fp
        return EditFileInput(
            file_path=str(fp.resolve()),
            old_string=input.old_string,
            new_string=input.new_string,
            replace_all=input.replace_all,
        )

    # ── Validation pipeline ───────────────────────────────────────

    async def validate_input(
        self, input: EditFileInput, context: dict[str, Any],
    ) -> tuple[bool, str]:
        """Stage 2: verify file exists, was read, and old_string differs from new_string.

        Validation order is deliberate: cheapest checks first, I/O last.
        P1 additions (§10.4): ErrCode 6 (unread file), ErrCode 7 (stale read).
        """
        # Step 0: Allowed write roots check (for restricted sub-agents).
        path = resolve_tool_path(input.file_path, context)
        ok, msg = check_allowed_write_roots(
            path,
            context.get("allowed_write_roots"),
        )
        if not ok:
            return False, _format_error(EditErrorCode.FILE_NOT_FOUND, msg)

        # Step 1: Reject no-op edits (cheapest check — no I/O).
        if input.old_string == input.new_string:
            return False, _format_error(
                EditErrorCode.NO_OP,
                "old_string and new_string must be different.",
            )

        # Step 2: File existence checks (requires stat).
        if not path.exists():
            hint = ""
            if not Path(input.file_path).is_absolute():
                hint = " (path is relative — use absolute paths)"
            return False, _format_error(
                EditErrorCode.FILE_NOT_FOUND,
                f"File not found: {input.file_path}{hint}",
            )
        if not path.is_file():
            return False, _format_error(
                EditErrorCode.FILE_NOT_FOUND,
                f"Not a file: {input.file_path}",
            )

        # Step 2a: Notebook redirect — P2.
        # .ipynb files must use notebook_edit, not edit_file.
        if path.suffix.lower() == ".ipynb":
            return False, _format_error(
                EditErrorCode.NOTEBOOK_REDIRECT,
                f"Notebook files must use notebook_edit tool: {input.file_path}",
            )

        # Step 3: Read-before-edit check — P1 (§10.4).
        # Unless old_string is empty (create-new-file semantic), the file
        # must have been read at least once in this session.
        # SKIPPED when ``skip_read_before_edit`` is set in context
        # (used by background extraction agents that write to memory dir).
        if input.old_string != "" and not context.get("skip_read_before_edit"):
            state = context.get("parent_state")
            if state is None or not hasattr(state, "read_file_state"):
                return False, _format_error(
                    EditErrorCode.UNREAD_FILE,
                    "File has not been read yet. Read it first before editing.",
                )
            file_state = state.read_file_state.get(str(path))
            if file_state is None:
                file_state = state.read_file_state.get(input.file_path)
            if file_state is None:
                return False, _format_error(
                    EditErrorCode.UNREAD_FILE,
                    "File has not been read yet. Read it first before editing.",
                )
            if file_state.is_partial_view:
                return False, _format_error(
                    EditErrorCode.UNREAD_FILE,
                    "File was only partially read. Re-read the full file before editing.",
                )

            # Step 4: External-modification detection — P1 (§10.4).
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                current_mtime = 0.0
            if current_mtime > file_state.timestamp:
                # Windows edge case: mtime may change without content
                # changes (cloud sync, antivirus). Compare content.
                if _is_windows():
                    try:
                        raw = path.read_bytes()
                        if raw[:2] == b'\xff\xfe':
                            current_content = raw.decode("utf-16-le", errors="replace")
                        else:
                            current_content = raw.decode("utf-8", errors="replace")
                        # Normalize both to LF for comparison
                        current_content = current_content.replace("\r\n", "\n")
                        cached_content = file_state.content.replace("\r\n", "\n")
                        if current_content == cached_content:
                            file_state.timestamp = current_mtime
                        else:
                            return False, _format_error(
                                EditErrorCode.STALE_READ,
                                "File has been modified since it was last read. "
                                "Re-read the file before editing.",
                            )
                    except Exception:
                        return False, _format_error(
                            EditErrorCode.STALE_READ,
                            "File has been modified since it was last read. "
                            "Re-read the file before editing.",
                        )
                else:
                    return False, _format_error(
                        EditErrorCode.STALE_READ,
                        "File has been modified since it was last read. "
                        "Re-read the file before editing.",
                    )

        return True, ""

    # ── Core execution ────────────────────────────────────────────

    async def execute(self, input: EditFileInput, context: dict[str, Any]) -> str:
        """Execute a search-and-replace edit with preprocessing.

        Pipeline:
          1. Read file content
          2. Preprocess new_string (trailing whitespace)
          3. Cascading-edit protection (P1: §10.5)
          4. Two-stage match (exact → quote-normalized)
          5. Uniqueness check (unless replace_all)
          6. Perform replacement + delete-newline heuristic
          7. Write to disk (preserving line endings: P1 §10.8)
          8. Update readFileState cache (P1 §10.4)
        """
        path = resolve_tool_path(input.file_path, context)

        # ── Step 1: Read file ─────────────────────────────────────
        try:
            raw_bytes = path.read_bytes()
            if raw_bytes[:2] == b'\xff\xfe':
                file_content = raw_bytes.decode("utf-16-le", errors="replace")
            else:
                file_content = raw_bytes.decode("utf-8", errors="replace")
            line_endings = detect_line_endings(file_content)
            # Normalize to LF-only internally so matching works
            # consistently regardless of the file's line endings.
            if line_endings == "\r\n":
                file_content = file_content.replace("\r\n", "\n")
        except Exception as e:
            return _format_error(
                EditErrorCode.READ_FAILED,
                f"Error reading file: {e}",
            )

        # ── Step 2: Preprocess new_string ─────────────────────────
        cleaned_new = _strip_trailing_whitespace(input.new_string, str(path))

        # ── Step 3: Cascading-edit protection (P1 §10.5) ─────────
        # Check that old_string is not a substring of any new_string
        # from a previous edit on the same file in this turn.
        cascading_error = _check_cascading_edit(
            context, str(path), input.old_string,
        )
        if cascading_error is not None:
            return cascading_error

        # ── Step 4: Two-stage match ────────────────────────────────
        actual_old = _find_actual_string(file_content, input.old_string)

        if actual_old is None:
            snippet = _match_failure_snippet(file_content, input.old_string)
            return _format_error(
                EditErrorCode.STRING_NOT_FOUND,
                f"String to replace not found in file.\n"
                f"  Searched for: {input.old_string[:120]}\n"
                f"  Hint: the file may have changed since you last read it.\n"
                f"{snippet}",
            )

        # ── Step 5: Uniqueness check ──────────────────────────────
        count = _count_occurrences(file_content, input.old_string)

        if not input.replace_all and count > 1:
            return _format_error(
                EditErrorCode.MULTIPLE_MATCHES,
                f"Found {count} matches of the string to replace, "
                f"but replace_all is false.\n"
                f"  To replace all occurrences, set replace_all=true.\n"
                f"  To replace only one, include more surrounding context "
                f"to make the match unique.",
            )

        # ── Step 6: Perform replacement ───────────────────────────
        actual_new = cleaned_new
        if actual_old != input.old_string:
            actual_new = _preserve_quote_style(
                input.old_string, actual_old, cleaned_new,
            )

        if actual_new == "" and not input.old_string.endswith("\n"):
            if (actual_old + "\n") in file_content:
                actual_old = actual_old + "\n"

        new_content = file_content.replace(actual_old, actual_new)

        # Track this edit for cascading protection on subsequent edits.
        _record_applied_edit(context, str(path), input.old_string, actual_new)

        # ── Step 7: Write to disk (preserving line endings) ─────
        # Always write bytes to avoid Python's platform-dependent
        # newline translation in text-mode write.
        try:
            if line_endings == "\r\n":
                new_content = new_content.replace("\n", "\r\n")
            path.write_bytes(new_content.encode("utf-8"))
            replaced = count if input.replace_all else 1

            # ── Step 8: Update readFileState cache ────────────────
            _update_read_state_after_edit(
                context, str(path), new_content,
            )

            # Generate diff for result rendering (P2).
            diff = generate_diff(
                str(path), file_content, new_content,
            )

            result = (
                f"Edit applied successfully: {replaced} occurrence(s) "
                f"replaced in {input.file_path}"
            )
            if diff:
                result += f"\n---DIFF---\n{diff}"
            return result
        except Exception as e:
            return _format_error(
                EditErrorCode.WRITE_FAILED,
                f"Error writing file: {e}",
            )


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════


def _match_failure_snippet(file_content: str, old_string: str) -> str:
    """Build a compact diagnostic snippet when old_string isn't found.

    Shows the best-matching line from the file to help the model
    understand WHY the match failed (whitespace drift, renamed
    identifiers, etc.).
    """
    old_first_line = old_string.split("\n")[0].strip()
    if not old_first_line:
        return ""

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
            f"  No similar lines found. Searched for: "
            f"'{old_first_line[:100]}'"
        )

    ctx_start = max(0, best_idx - 2)
    ctx_end = min(len(file_lines), best_idx + 2)

    lines = [
        f"  Closest match at line {best_idx + 1} "
        f"({best_score:.0%} similarity):",
    ]
    for i in range(ctx_start, ctx_end):
        marker = ">>>" if i == best_idx else "   "
        line_text = file_lines[i][:120]
        lines.append(f"  {marker} L{i + 1:4d}  {line_text}")

    return "\n".join(lines)


def _line_similarity(a: str, b: str) -> float:
    """Simple token-based line similarity for fuzzy matching."""
    if a == b:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))


def _preserve_quote_style(
    original_search: str,
    actual_from_file: str,
    new_string: str,
) -> str:
    """When quote normalization was used, convert new_string's quotes back.

    If the file uses curly quotes and normalization allowed the match,
    convert new_string's straight quotes to curly so the file stays
    typographically consistent after the edit.

    Uses a simple heuristic: each quote in actual_from_file maps
    to the corresponding quote position in original_search, and we
    apply the same mapping to new_string.
    """
    # Build a mapping: straight → curly for each quote character
    quote_map: dict[str, str] = {}
    for oc, ac in zip(original_search, actual_from_file):
        if oc != ac and oc in ('"', "'") and ac in ('“', '”', '‘', '’'):
            quote_map[oc] = ac

    if not quote_map:
        return new_string

    result_chars: list[str] = []
    for ch in new_string:
        result_chars.append(quote_map.get(ch, ch))
    return "".join(result_chars)


# ═════════════════════════════════════════════════════════════════════
# P1 helpers: platform detection, cascading-edit protection,
# readFileState update after edit
# ═════════════════════════════════════════════════════════════════════


def _is_windows() -> bool:
    """Check if running on Windows (for mtime vs content comparison)."""
    return _sys.platform == "win32"


def _check_cascading_edit(
    context: dict[str, Any], file_path: str, old_string: str,
) -> str | None:
    """Check if old_string is a substring of a previous edit's new_string.

    P1 cascading-edit protection (§10.5): if edit A inserts text that
    edit B later tries to match, the result is a corrupted edit.  This
    check prevents the second edit from silently matching inserted text.
    """
    applied_edits: list[dict] = context.get("_applied_edits", {}).get(file_path, [])
    if not applied_edits:
        return None

    old_trimmed = old_string.rstrip("\n")
    if not old_trimmed:
        return None

    for prev in applied_edits:
        prev_new: str = prev.get("new_string", "")
        if old_trimmed in prev_new:
            return _format_error(
                EditErrorCode.CASCADING_EDIT,
                "Cascading edit detected: old_string is a substring of a "
                "new_string from a previous edit on this file. "
                "Re-read the file and adjust the edit to target the "
                "original content.",
            )

    return None


def _record_applied_edit(
    context: dict[str, Any], file_path: str,
    old_string: str, new_string: str,
) -> None:
    """Track an applied edit for cascading protection on subsequent edits."""
    if "_applied_edits" not in context:
        context["_applied_edits"] = {}
    if file_path not in context["_applied_edits"]:
        context["_applied_edits"][file_path] = []
    context["_applied_edits"][file_path].append({
        "old_string": old_string,
        "new_string": new_string,
    })


def _update_read_state_after_edit(
    context: dict[str, Any], file_path: str, new_content: str,
) -> None:
    """Update readFileState after a successful edit.

    Stores LF-normalized content so that mtime-vs-content comparison
    in validate_input works correctly regardless of platform line endings.
    """
    state = context.get("parent_state")
    if state is None or not hasattr(state, "read_file_state"):
        return
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        mtime = 0.0
    line_endings = detect_line_endings(new_content)
    # Normalize to LF for consistent content comparison
    if line_endings == "\r\n":
        new_content = new_content.replace("\r\n", "\n")
    state.read_file_state[file_path] = FileStateEntry(
        content=new_content,
        timestamp=mtime,
        is_partial_view=False,
        line_endings=line_endings,
    )
