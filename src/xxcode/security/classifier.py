"""Bash command classifier — speculative auto-approval for safe commands.

Integrates into the tool execution pipeline so safe shell commands
(ls, cat, find, grep, echo, etc.) skip the interactive permission
prompt entirely, reducing user friction without compromising safety.

Enhanced with:
  - Safe environment variable stripping (26 vars)
  - Zsh dangerous command awareness
  - Better subcommand detection for composite commands
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from ..tools.BashTool.security import run_all_security_checks, is_blocking
from ..tools.BashTool._tokenizer import (
    extract_base_command as _canonical_extract_base_command,
    SAFE_ENV_VARS,
    normalize_base_token as _canonical_normalize_base_token,
    split_pipeline as _canonical_split_pipeline,
    strip_all_safe_env_prefixes as _canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as _canonical_strip_safe_env_vars,
    tokenize as _canonical_tokenize,
)
from .patterns import is_dangerous


class CommandClass(Enum):
    SAFE = auto()             # Auto-approve — read-only, no side effects
    NEEDS_PERMISSION = auto() # Ask user — may write or have side effects
    DANGEROUS = auto()        # Always confirm — destructive potential


# ── Safe environment variables (harmless to strip) ───────────────────

_PRIVILEGE_PREFIXES = ("sudo", "doas", "pkexec")
# Defensive stop tokens: _split_pipeline() should already separate these,
# but we still stop on them here to stay fail-closed on unsplit input.
_COMMAND_STOP_TOKENS = {
    ">",
    ">>",
    "<",
    "2>",
    "1>",
    "&>",
    "2>&1",
    "1>&2",
    "|",
    ";",
    "&",
    "&&",
    "||",
}
_DANGEROUS_ENV_PREFIXES = {"IFS"}
_SENSITIVE_READ_TARGETS = {
    "/etc/shadow",
    "/etc/sudoers",
}
_SECRET_READ_TARGETS = {
    "/proc/self/environ",
    "/proc/1/environ",
}


# Commands that are always safe (read-only, no side effects).
_SAFE_COMMANDS: set[str] = {
    # File reading / inspection
    "ls", "dir", "cat", "head", "tail", "less", "more",
    "file", "stat", "wc", "du", "df", "tree",
    # Text search / processing
    "grep", "rg", "find", "locate", "which", "whereis", "where",
    "awk", "sed", "sort", "uniq", "cut", "tr", "column",
    # System info
    "echo", "printf", "date", "uptime", "uname", "hostname",
    "whoami", "id", "groups", "env", "printenv", "pwd",
    "type", "command", "help", "man",
    # Version managers (info)
    "nvm", "pyenv", "rbenv", "sdk",
}
# Additional prefixes for commands like "git log", "npm list", etc.
_SAFE_SUBCOMMANDS: dict[str, set[str]] = {
    "git": {
        "status", "log", "diff", "show", "branch", "tag",
        "blame", "stash", "remote", "config", "rev-parse",
        "ls-files", "ls-tree", "describe", "shortlog",
        "cherry", "grep", "reflog", "whatchanged",
    },
    "npm": {"ls", "list", "view", "info", "outdated", "audit", "root", "bin"},
    "yarn": {"list", "info", "why", "audit", "outdated"},
    "pnpm": {"list", "outdated", "audit", "why"},
    "pip": {"list", "show", "freeze", "check"},
    "pip3": {"list", "show", "freeze", "check"},
    "docker": {"ps", "images", "inspect", "logs", "stats", "version", "info"},
    "docker-compose": {"ps", "images", "logs", "config", "port"},
    "kubectl": {"get", "describe", "logs", "explain", "api-versions", "cluster-info"},
    "systemctl": {"status", "list-units", "is-enabled", "is-active"},
    "journalctl": set(),  # Read-only by default
    "gh": {"pr", "issue", "repo", "status", "auth"},
    "poetry": {"show", "check", "env", "list"},
    "cargo": {"check", "build", "test", "doc", "clippy"},
    "go": {"build", "test", "vet", "fmt", "list", "mod"},
}


@dataclass
class ClassifierResult:
    """Output of the bash command classifier."""
    command_class: CommandClass
    safe_command: str | None = None    # Extracted base command (e.g. "git")
    reason: str = ""                   # Human-readable classification reason


def classify_command(command: str) -> ClassifierResult:
    """Classify a shell command as SAFE, NEEDS_PERMISSION, or DANGEROUS.

    This is the main entry point for speculative execution — SAFE commands
    skip the permission prompt; NEEDS_PERMISSION and DANGEROUS fall through
    to the interactive permission chain.

    Strategy:
      1. Extract the base command (first word after stripping env vars / redirects).
      2. Check if it's a known-safe base command.
      3. For compound commands (git X, npm Y), check the subcommand.
      4. Fall back to is_dangerous() pattern matching for DANGEROUS.
      5. Everything else is NEEDS_PERMISSION.
    """
    cleaned = command.strip()

    # Handle chained commands (&&, ||, ;, |) — if any segment is
    # non-safe, the whole command needs permission.
    segments = _split_pipeline(cleaned)
    if len(segments) > 1:
        for seg in segments:
            result = classify_command(seg)
            if result.command_class != CommandClass.SAFE:
                return ClassifierResult(CommandClass.NEEDS_PERMISSION, reason="pipeline contains non-safe command")
        return ClassifierResult(CommandClass.SAFE, reason="all pipeline segments are safe")

    security_result = run_all_security_checks(cleaned)
    if is_blocking(security_result):
        return ClassifierResult(CommandClass.DANGEROUS, reason="blocked by shell security checks")

    # Extract base command (first non-redirect, non-env-var word).
    base, subcommand, has_sudo = _extract_base_command(cleaned)

    if not base:
        return ClassifierResult(CommandClass.NEEDS_PERMISSION, reason="could not parse command")

    # sudo/doas/pkexec always escalate to DANGEROUS regardless of the
    # underlying command — running anything as root is a privilege boundary.
    if has_sudo:
        return ClassifierResult(CommandClass.DANGEROUS, reason="sudo/doas elevates privileges")

    if base in _DANGEROUS_ENV_PREFIXES:
        return ClassifierResult(CommandClass.DANGEROUS, reason=f"{base} assignment changes shell parsing")

    if "=" in base:
        return ClassifierResult(CommandClass.NEEDS_PERMISSION, reason="command has unsafe env prefix")

    if _has_output_redirection(cleaned):
        return ClassifierResult(CommandClass.NEEDS_PERMISSION, reason="command writes output")

    sensitive_read = _classify_sensitive_read(base, cleaned)
    if sensitive_read is not None:
        return sensitive_read

    # Check if the base command itself is safe.
    if base in _SAFE_COMMANDS:
        return ClassifierResult(CommandClass.SAFE, safe_command=base, reason=f"'{base}' is read-only")

    # Check compound command (git status, npm list, etc.).
    if base in _SAFE_SUBCOMMANDS:
        safe_subs = _SAFE_SUBCOMMANDS[base]
        if subcommand and subcommand in safe_subs:
            return ClassifierResult(
                CommandClass.SAFE, safe_command=base,
                reason=f"'{base} {subcommand}' is read-only",
            )

    # Check dangerous patterns.
    if is_dangerous(cleaned):
        return ClassifierResult(CommandClass.DANGEROUS, reason="matches dangerous pattern")

    return ClassifierResult(CommandClass.NEEDS_PERMISSION, reason="may have side effects")


def strip_safe_env_vars(command: str) -> str:
    """Strip safe environment variable assignments from a command prefix.

    NODE_ENV=prod npm run build → npm run build
    LD_PRELOAD=evil.so curl → unchanged (LD_PRELOAD is NOT safe)
    """
    previous = None
    current = command
    while previous != current:
        previous = current
        current = _canonical_strip_safe_env_vars(current)
    return current


def is_safe_command(command: str) -> bool:
    """Convenience: returns True if the command can auto-approve."""
    return classify_command(command).command_class == CommandClass.SAFE


# ── Internal helpers ─────────────────────────────────────────────────


def _split_pipeline(command: str) -> list[str]:
    """Split a command by shell control operators using the canonical helper."""
    return _canonical_split_pipeline(command)


def _extract_base_command(command: str) -> tuple[str | None, str | None, bool]:
    """Extract the base command, subcommand, and sudo-flag from a shell command line.

    Strips env-var assignments (FOO=bar), redirects (>/dev/null, 2>&1).

    Returns (base_command, subcommand_or_None, has_sudo).
    """
    cleaned = _canonical_strip_all_safe_env_prefixes(command.strip())
    tokens = _tokenize_command(cleaned)

    filtered: list[str] = []
    for token in tokens:
        if token in _COMMAND_STOP_TOKENS:
            break
        filtered.append(token)

    if not filtered:
        return None, None, False

    has_sudo = filtered[0] in _PRIVILEGE_PREFIXES
    idx = 1 if has_sudo else 0
    if idx >= len(filtered):
        return None, None, has_sudo

    base_token = filtered[idx]
    if "=" in base_token:
        base = base_token
    else:
        base = _canonical_extract_base_command(cleaned) or _canonical_normalize_base_token(base_token)
    subcommand = filtered[idx + 1] if idx + 1 < len(filtered) else None

    return base, subcommand, has_sudo


def _tokenize_command(command: str) -> list[str]:
    """Tokenize a command using the canonical shell tokenizer."""
    return _canonical_tokenize(command)


def _classify_sensitive_read(base: str, command: str) -> ClassifierResult | None:
    if base not in {"cat", "head", "tail", "less", "more", "grep", "rg", "awk", "sed"}:
        return None
    tokens = _tokenize_command(command)
    targets = {
        token
        for token in tokens[1:]
        if token and not token.startswith("-")
    }
    if targets & _SECRET_READ_TARGETS:
        return ClassifierResult(
            CommandClass.DANGEROUS,
            safe_command=base,
            reason="reads process environment secrets",
        )
    if targets & _SENSITIVE_READ_TARGETS:
        return ClassifierResult(
            CommandClass.NEEDS_PERMISSION,
            safe_command=base,
            reason="reads sensitive system file",
        )
    return None


def _has_output_redirection(command: str) -> bool:
    tokens = _tokenize_command(command)
    return any(token in {">", ">>", "1>", "2>", "&>"} for token in tokens)
