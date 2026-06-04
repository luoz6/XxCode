"""Command exit code semantics — interpret non-standard exit conventions.

Standard Unix convention: exit 0 = success, non-0 = error.
But many common tools violate this convention.  This module interprets
exit codes based on command identity so the model doesn't mistake
"grep found no matches" for a tool execution failure and retry.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._tokenizer import extract_base_command, split_pipeline


# ── Exit code interpretation tables ───────────────────────────────────

@dataclass
class ExitCodeInterpretation:
    """Interpretation of a command's exit code."""
    is_error: bool       # True = actual failure, False = expected/benign
    description: str     # Human-readable explanation


# Per-command exit code mappings.
# Keys: command name → {exit_code: interpretation}
_EXIT_CODE_TABLES: dict[str, dict[int, ExitCodeInterpretation]] = {
    "grep": {
        0: ExitCodeInterpretation(False, "Match(es) found"),
        1: ExitCodeInterpretation(False, "No match found (not an error)"),
        2: ExitCodeInterpretation(True, "Error: invalid pattern or file"),
    },
    "rg": {
        0: ExitCodeInterpretation(False, "Match(es) found"),
        1: ExitCodeInterpretation(False, "No match found (not an error)"),
        2: ExitCodeInterpretation(True, "Error: invalid pattern or file"),
    },
    "diff": {
        0: ExitCodeInterpretation(False, "Files are identical"),
        1: ExitCodeInterpretation(False, "Files differ (not an error)"),
        2: ExitCodeInterpretation(True, "Error: cannot compare files"),
    },
    "test": {
        0: ExitCodeInterpretation(False, "Condition is true"),
        1: ExitCodeInterpretation(False, "Condition is false (not an error)"),
        2: ExitCodeInterpretation(True, "Syntax error in test expression"),
    },
    "[": {
        0: ExitCodeInterpretation(False, "Condition is true"),
        1: ExitCodeInterpretation(False, "Condition is false (not an error)"),
        2: ExitCodeInterpretation(True, "Syntax error in test expression"),
    },
    "find": {
        0: ExitCodeInterpretation(False, "Success"),
        1: ExitCodeInterpretation(True, "Partial failure (some dirs inaccessible)"),
    },
    "cmp": {
        0: ExitCodeInterpretation(False, "Files are identical"),
        1: ExitCodeInterpretation(False, "Files differ (not an error)"),
        2: ExitCodeInterpretation(True, "Error: cannot compare files"),
    },
    "expr": {
        0: ExitCodeInterpretation(False, "Expression is non-zero/non-null"),
        1: ExitCodeInterpretation(False, "Expression is zero or null"),
        2: ExitCodeInterpretation(True, "Syntax error in expression"),
        3: ExitCodeInterpretation(True, "Error: internal error"),
    },
    "pgrep": {
        0: ExitCodeInterpretation(False, "Process(es) found"),
        1: ExitCodeInterpretation(False, "No process found (not an error)"),
        2: ExitCodeInterpretation(True, "Error: invalid pattern or option"),
        3: ExitCodeInterpretation(True, "Error: internal error"),
    },
    "which": {
        0: ExitCodeInterpretation(False, "Command found"),
        1: ExitCodeInterpretation(False, "Command not found (not an error)"),
    },
    "type": {
        0: ExitCodeInterpretation(False, "Command found"),
        1: ExitCodeInterpretation(False, "Command not found (not an error)"),
    },
    "command": {
        0: ExitCodeInterpretation(False, "Command exists"),
        1: ExitCodeInterpretation(False, "Command not found (not an error)"),
    },
    "git": {
        # git grep returns 1 for no matches, like grep.
        0: ExitCodeInterpretation(False, "Success"),
        1: ExitCodeInterpretation(False, "No matches or minor warning"),
        128: ExitCodeInterpretation(True, "Fatal error"),
    },
    "rsync": {
        0: ExitCodeInterpretation(False, "Success"),
        23: ExitCodeInterpretation(True, "Partial transfer (some files failed)"),
        24: ExitCodeInterpretation(True, "Partial transfer (source files vanished)"),
    },
}


def resolve_base_command(command_line: str) -> str | None:
    """Extract the base command name from a command line — delegates to shared _tokenizer."""
    return extract_base_command(command_line)


def interpret_command_result(command_line: str, exit_code: int) -> ExitCodeInterpretation:
    """Interpret a command's exit code based on the command identity.

    Args:
        command_line: The full command line (used to identify the command).
        exit_code: The exit code from the process.

    Returns:
        ExitCodeInterpretation with is_error flag and description.
    """
    if exit_code == 0:
        return ExitCodeInterpretation(False, "Success (exit 0)")

    base = resolve_base_command(command_line)
    if not base:
        return ExitCodeInterpretation(exit_code != 0, f"Exit code {exit_code}")

    # Look up the specific command's exit codes.
    table = _EXIT_CODE_TABLES.get(base)
    if table is not None:
        specific = table.get(exit_code)
        if specific is not None:
            return specific
        # Check for wildcard pattern: some commands treat any non-0 as success.
        # Fall through to generic handling.

    # Generic: non-zero = error by default.
    if exit_code < 0:
        return ExitCodeInterpretation(
            True, f"Killed by signal {-exit_code}",
        )
    return ExitCodeInterpretation(
        True, f"Error (exit code {exit_code})",
    )


def format_exit_code(command_line: str, exit_code: int) -> str:
    """Format an exit code for display with command-specific interpretation.

    Returns a user-friendly string like:
      "[Exit code: 1 — No match found]"
    """
    interpretation = interpret_command_result(command_line, exit_code)
    if interpretation.is_error:
        return f"[Exit code: {exit_code} — ERROR: {interpretation.description}]"
    else:
        return f"[Exit code: {exit_code} — {interpretation.description}]"


# ── Command classification for UI ─────────────────────────────────────

# Commands that can be folded in the UI display.
_SEARCH_COMMANDS: set[str] = {
    "find", "grep", "rg", "ag", "ack", "locate", "which", "whereis",
}

_READ_COMMANDS: set[str] = {
    "cat", "head", "tail", "less", "more", "wc", "stat", "file",
    "jq", "awk", "cut", "sort", "uniq", "tr",
}

_LIST_COMMANDS: set[str] = {
    "ls", "tree", "du",
}

_SEMANTIC_NEUTRAL_COMMANDS: set[str] = {
    "echo", "printf", "true", "false", ":",
}


def is_search_or_read_bash_command(command: str) -> bool:
    """Determine if a bash command is purely search/read for UI folding.

    Analyzes every segment of a pipeline — only folds if ALL
    segments are search or read commands.  Semantic-neutral
    commands (echo, true) are ignored.
    """
    segments = split_pipeline(command)
    meaningful_segments = 0

    for seg in segments:
        base, _ = _parse_base(seg)
        if base in _SEMANTIC_NEUTRAL_COMMANDS:
            continue
        meaningful_segments += 1
        if base not in _SEARCH_COMMANDS and base not in _READ_COMMANDS and base not in _LIST_COMMANDS:
            return False

    return meaningful_segments > 0


def classify_bash_command(command: str) -> str:
    """Classify a bash command for UI display.

    Returns one of: 'search', 'read', 'list', 'write', 'unknown'.
    """
    segments = split_pipeline(command)
    categories: set[str] = set()

    for seg in segments:
        base, _ = _parse_base(seg)
        if base in _SEMANTIC_NEUTRAL_COMMANDS:
            continue
        if base in _SEARCH_COMMANDS:
            categories.add("search")
        elif base in _READ_COMMANDS:
            categories.add("read")
        elif base in _LIST_COMMANDS:
            categories.add("list")
        else:
            categories.add("write")

    if "write" in categories:
        return "write"
    if "search" in categories and not categories - {"search", "read"}:
        return "search"
    if "read" in categories:
        return "read"
    if "list" in categories:
        return "list"
    return "unknown"


def _parse_base(command: str) -> tuple[str | None, str | None]:
    """Parse base command and subcommand from a command segment."""
    tokens = command.strip().split()
    if not tokens:
        return None, None
    base = tokens[0].rsplit("/", 1)[-1]
    sub = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") else None
    return base, sub
