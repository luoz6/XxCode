"""Path validation and destructive command warnings.

Extracts path arguments from 24 command types and validates they are
within the allowed working directory.  Includes hard safety limits
for rm -rf / and destructive command pattern detection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._tokenizer import tokenize


# ── Commands that accept file/directory arguments ─────────────────────

# Mapping: command name → path extraction strategy
#   "first_arg"   — first non-flag argument
#   "all_args"    — all non-flag arguments
#   "cd"          — cd-specific (join all args into one path)
#   "find"        — find-specific (paths before first flag)
#   "grep"        — grep-specific (files after pattern)

_PATH_COMMAND_STRATEGIES: dict[str, str] = {
    "cd": "cd",
    "rm": "all_args",
    "rmdir": "all_args",
    "mv": "all_args",
    "cp": "all_args",
    "cat": "all_args",
    "head": "all_args",
    "tail": "all_args",
    "less": "all_args",
    "more": "all_args",
    "grep": "grep",
    "rg": "grep",
    "find": "find",
    "touch": "all_args",
    "mkdir": "all_args",
    "chmod": "all_args",
    "chown": "chown",
    "ln": "all_args",
    "stat": "all_args",
    "file": "all_args",
    "du": "all_args",
    "df": "all_args",
    "source": "first_arg",
    ".": "first_arg",
    "code": "all_args",
    "vim": "all_args",
    "nvim": "all_args",
    "nano": "all_args",
    "emacs": "all_args",
    "python": "first_arg",
    "python3": "first_arg",
    "node": "first_arg",
}


# ── Destructive command patterns ──────────────────────────────────────

@dataclass
class DestructiveWarning:
    """A warning about a potentially destructive command."""
    category: str
    message: str
    pattern: str  # What matched


DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Category → (pattern, message)
    (re.compile(r'git\s+reset\s+--hard', re.IGNORECASE),
     "Git data loss", "may discard uncommitted changes"),
    (re.compile(r'git\s+push\s+.*(?:--force|-f\b)', re.IGNORECASE),
     "Git history overwrite", "may overwrite remote history"),
    (re.compile(r'--no-verify', re.IGNORECASE),
     "Git safety bypass", "may skip safety hooks"),
    (re.compile(r'git\s+commit\s+--amend', re.IGNORECASE),
     "Git commit overwrite", "may rewrite the last commit"),
    (re.compile(r'rm\s+.*(?:-r|-rf|--recursive)', re.IGNORECASE),
     "Recursive force delete", "may recursively force-remove files"),
    (re.compile(r'\bDROP\s+TABLE', re.IGNORECASE),
     "Database drop", "may drop database objects"),
    (re.compile(r'\bTRUNCATE\s+', re.IGNORECASE),
     "Database truncate", "may truncate database objects"),
    (re.compile(r'\bDELETE\s+FROM\s+\w+\s*;', re.IGNORECASE),
     "Database delete (no WHERE)", "may delete all rows"),
    (re.compile(r'\bkubectl\s+delete\b', re.IGNORECASE),
     "Kubernetes delete", "may delete Kubernetes resources"),
    (re.compile(r'\bterraform\s+destroy\b', re.IGNORECASE),
     "Terraform destroy", "may destroy Terraform infrastructure"),
    (re.compile(r'\bDROP\s+DATABASE', re.IGNORECASE),
     "Database drop", "may drop entire database"),
    (re.compile(r'\bdocker\s+rm\b', re.IGNORECASE),
     "Docker remove", "may remove Docker containers"),
    (re.compile(r'\bdocker\s+rmi\b', re.IGNORECASE),
     "Docker remove image", "may remove Docker images"),
    (re.compile(r'\bdocker\s+system\s+prune\b', re.IGNORECASE),
     "Docker prune", "may remove unused Docker data"),
    (re.compile(r':\s*\(\)\s*\{', re.IGNORECASE),
     "Fork bomb", "fork bomb pattern detected"),
]


def check_destructive_warnings(command: str) -> list[DestructiveWarning]:
    """Check a command for destructive patterns.

    Returns list of warnings (informational only — does not block).
    """
    warnings: list[DestructiveWarning] = []
    for pattern, category, message in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            warnings.append(DestructiveWarning(
                category=category,
                message=message,
                pattern=pattern.pattern,
            ))
    return warnings


# ── rm -rf / hard safety limit ────────────────────────────────────────

# These patterns are NEVER auto-approved, regardless of saved rules.
_ABSOLUTE_DESTROY_RE = re.compile(
    r'\brm\s+.*(?:-r|-rf|--recursive)\s+(?:/\s|/[\*]|~)',
    re.IGNORECASE,
)


def is_absolute_destroy(command: str) -> bool:
    """Check if the command is rm -rf / or rm -rf ~.

    These are hard-blocked — no saved rule can override.
    """
    return bool(_ABSOLUTE_DESTROY_RE.search(command))


# ── Path extraction ───────────────────────────────────────────────────

def extract_paths(command: str, cwd: Path | None = None) -> list[str]:
    """Extract file/directory paths from a command based on its type.

    Args:
        command: The shell command string.
        cwd: Current working directory for relative path resolution.

    Returns:
        List of extracted path strings.
    """
    base, tokens = _parse_command_structure(command)
    if not base:
        return []

    strategy = _PATH_COMMAND_STRATEGIES.get(base, "")
    if not strategy:
        return []

    if strategy == "cd":
        return _extract_cd_paths(tokens)
    elif strategy == "find":
        return _extract_find_paths(tokens)
    elif strategy == "grep":
        return _extract_grep_paths(tokens)
    elif strategy == "chown":
        return _extract_chown_paths(tokens)
    elif strategy == "all_args":
        return _extract_all_arg_paths(tokens)
    elif strategy == "first_arg":
        return _extract_first_arg_path(tokens)

    return []


def _parse_command_structure(command: str) -> tuple[str | None, list[str]]:
    """Parse a command into base command + token list.

    Strips env vars and resolves path-prefixed commands.
    Respects POSIX -- separator.
    """
    tokens = _tokenize(command)
    if not tokens:
        return None, []

    # Filter env assignments from the front.
    idx = 0
    while idx < len(tokens) and re.match(r'^[A-Za-z_]\w*=', tokens[idx]):
        idx += 1

    if idx >= len(tokens):
        return None, []

    base = tokens[idx].split("/")[-1]  # /usr/bin/git → git
    return base, tokens[idx + 1:]


def _extract_cd_paths(tokens: list[str]) -> list[str]:
    """cd joins all non-flag arguments into one path."""
    parts: list[str] = []
    for t in tokens:
        if t == "--":
            continue
        if t.startswith("-"):
            continue
        parts.append(t)
    if parts:
        return [" ".join(parts)]
    return []


def _extract_find_paths(tokens: list[str]) -> list[str]:
    """find takes paths before the first expression flag."""
    paths: list[str] = []
    for t in tokens:
        if t == "--":
            continue
        # Stop at find expressions (flags starting with - except -print, -print0).
        if t.startswith("-") and t not in ("-print", "-print0"):
            # Check if it's actually a negative number (not a flag).
            try:
                float(t)
                paths.append(t)
                continue
            except ValueError:
                break
        if not t.startswith("-"):
            paths.append(t)
    return paths


def _extract_grep_paths(tokens: list[str]) -> list[str]:
    """grep/rg takes pattern first, then file paths."""
    paths: list[str] = []
    pattern_found = False
    skip_next = False

    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t == "--":
            # After --, everything is a file path.
            continue
        if t in ("-e", "-f", "--regexp", "--file"):
            # Next token is the pattern/value for this flag.
            if not pattern_found:
                pattern_found = True
            skip_next = True
            continue
        if t.startswith("-"):
            continue
        if not pattern_found:
            pattern_found = True  # First non-flag token is the pattern.
            continue
        paths.append(t)

    return paths


def _extract_chown_paths(tokens: list[str]) -> list[str]:
    """chown takes owner[:group] first, then file paths."""
    owner_found = False
    paths: list[str] = []
    for t in tokens:
        if t == "--":
            continue
        if t.startswith("-"):
            continue
        if not owner_found:
            owner_found = True
            continue
        paths.append(t)
    return paths


def _extract_all_arg_paths(tokens: list[str]) -> list[str]:
    """Extract all non-flag, non-redirect tokens as paths."""
    paths: list[str] = []
    past_dashdash = False
    for t in tokens:
        if t == "--":
            past_dashdash = True
            continue
        if not past_dashdash and t.startswith("-"):
            continue
        # Skip redirect operators and their targets.
        if t in (">", ">>", "<", "2>", "1>", "&>", "|"):
            continue
        paths.append(t)
    return paths


def _extract_first_arg_path(tokens: list[str]) -> list[str]:
    """Extract the first non-flag argument as the path."""
    for t in tokens:
        if t == "--":
            continue
        if t.startswith("-"):
            continue
        return [t]
    return []


def _tokenize(command: str) -> list[str]:
    """Tokenize a command — delegates to shared tokenizer."""
    return tokenize(command)


# ── Path safety check ─────────────────────────────────────────────────

def is_path_within_workspace(path: str, workspace_root: str) -> bool:
    """Check if a path is within the allowed workspace.

    Uses path resolution, not string prefix matching, to avoid
    false positives on sibling directories like /home/user/proj_evil.
    """
    try:
        resolved = Path(path).resolve()
        root = Path(workspace_root).resolve()
        # Use relative_to for proper containment check.
        resolved.relative_to(root)
        return True
    except ValueError:
        return False
    except (OSError, RuntimeError):
        return False


def validate_paths(
    command: str, workspace_root: str,
) -> tuple[bool, list[str]]:
    """Validate all paths in a command are within the workspace.

    Returns:
        (all_valid, invalid_paths)
    """
    paths = extract_paths(command)
    invalid: list[str] = []

    for p in paths:
        # Skip absolute system paths that are always safe to read.
        if p.startswith("/dev/") or p.startswith("/proc/") or p.startswith("/sys/"):
            continue
        # Skip special paths.
        if p in ("-", "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            continue
        if not is_path_within_workspace(p, workspace_root):
            # Check if it might be a relative path within workspace.
            try:
                full = (Path(workspace_root) / p).resolve()
                if not str(full).startswith(str(Path(workspace_root).resolve())):
                    invalid.append(p)
            except (ValueError, OSError):
                invalid.append(p)

    return len(invalid) == 0, invalid
