"""Multi-layer permission system for bash commands.

Port of Claude Code's bashPermissions.ts — the most complex permission
function in the codebase.  Implements a 5-step layered analysis:

  1. AST parsing + complexity classification
  2. Subcommand splitting with hard cap (50)
  3. Safe environment variable stripping (26 vars)
  4. Prefix extraction for rule suggestions
  5. Compound command permission aggregation
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._tokenizer import (
    split_pipeline as _split_compound,
    strip_all_safe_env_prefixes as _canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as _canonical_strip_safe_env_vars,
    tokenize,
)


# ── Constants ─────────────────────────────────────────────────────────

MAX_SUBCOMMANDS_FOR_SECURITY_CHECK = 50
MAX_SUGGESTED_RULES_FOR_COMPOUND = 5


class ParseResult(Enum):
    SIMPLE = "simple"
    TOO_COMPLEX = "too-complex"
    PARSE_UNAVAILABLE = "parse-unavailable"


# ── Safe environment variables ────────────────────────────────────────
#
# These 26 env vars are known-harmless and are stripped before matching
# permission rules.  e.g. NODE_ENV=prod npm run build → npm run build.
#
# Variables like LD_PRELOAD are NOT in this list — they can change
# command behaviour and must not be stripped unconditionally.

SAFE_ENV_VARS: set[str] = {
    # Go
    "GOEXPERIMENT", "GOOS", "GOARCH", "GOPATH", "GOROOT",
    "GOPROXY", "GOMODCACHE", "GONOSUMCHECK", "GONOSUMDB", "GOPRIVATE",
    # Rust
    "RUST_BACKTRACE", "RUST_LOG", "RUSTFLAGS",
    # Node
    "NODE_ENV", "NODE_OPTIONS",
    # Python
    "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONWARNINGS",
    # Locale
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME",
    # Common
    "HOME", "USER", "PATH", "TERM", "SHELL",
    # CI
    "CI", "GITHUB_ACTIONS", "GITLAB_CI",
    # Display
    "DISPLAY", "WAYLAND_DISPLAY",
    # Editor
    "EDITOR", "VISUAL", "PAGER",
}


# ── Risk classification ───────────────────────────────────────────────

class Risk(Enum):
    SAFE = "safe"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PermissionResult:
    """Output of the multi-layer permission analysis."""
    allowed: bool = False
    risk: Risk = Risk.NORMAL
    reason: str = ""
    suggested_rules: list[str] = field(default_factory=list)
    needs_user_decision: bool = True
    parse_result: ParseResult = ParseResult.PARSE_UNAVAILABLE


# ── Command splitting ─────────────────────────────────────────────────
# Uses shared _tokenizer.split_pipeline (canonical implementation).


# ── Safe env var stripping ────────────────────────────────────────────

def strip_safe_env_vars(command: str) -> str:
    """Strip safe environment variable assignments from the command.

    NODE_ENV=prod npm run build → npm run build
    LD_PRELOAD=evil.so curl → LD_PRELOAD=evil.so curl (NOT stripped — unsafe)
    """
    return _canonical_strip_safe_env_vars(command)


def strip_all_safe_env_prefixes(command: str) -> str:
    """Repeatedly strip safe env var prefixes."""
    return _canonical_strip_all_safe_env_prefixes(command)


# ── Command prefix extraction ─────────────────────────────────────────

# Prefixes that should NOT generate rule suggestions because they
# would be equivalent to Bash(*) (approve everything).
_BLOCKED_RULE_PREFIXES: set[str] = {
    "bash", "sh", "zsh", "dash", "ksh", "sudo", "doas", "pkexec",
    "env", "exec", "su", "nohup", "nice",
}


def get_simple_command_prefix(command: str) -> str | None:
    """Extract a stable 2-word prefix for reusable permission rules.

    Examples:
        git commit -m "msg"     → "git commit"    → Bash(git commit:*)
        npm run build           → "npm run"       → Bash(npm run:*)
        NODE_ENV=prod npm run   → "npm run"       → (safe env stripped)
        ls -la                  → None            → (flags only, no prefix rule)
        bash -c "rm -rf /"      → None            → (blocked prefix)
        sudo make install       → None            → (blocked prefix)

    Returns None when:
      - The command is a blocked prefix (bash/sh/sudo/env etc.)
      - There is no second token (single-word command)
      - The second token starts with - (a flag, not a subcommand)
    """
    cleaned = strip_all_safe_env_prefixes(command)
    tokens = tokenize_command(cleaned)

    if not tokens:
        return None

    # Block dangerous prefixes.
    base = tokens[0].split("/")[-1]  # /usr/bin/git → git
    if base in _BLOCKED_RULE_PREFIXES:
        return None

    if len(tokens) < 2:
        return None

    second = tokens[1]
    # Don't suggest prefix rules for flags.
    if second.startswith("-"):
        return None

    # Don't suggest for certain subcommand patterns.
    if base == "git" and second == "-c":
        # git -c key=val ... → strip the -c pair, try next token
        if len(tokens) >= 4:
            return f"git {tokens[3]}"
        return None

    return f"{base} {second}"


def tokenize_command(command: str) -> list[str]:
    """Tokenize a command respecting quotes — delegates to shared _tokenizer."""
    return tokenize(command)


# ── Compound command permission aggregation ───────────────────────────

def aggregate_compound_permissions(
    sub_results: list[PermissionResult],
) -> PermissionResult:
    """Aggregate per-subcommand permission results.

    All must pass independently.  Any deny → overall deny.
    Any ask → overall ask.  All safe → overall safe.
    """
    if not sub_results:
        return PermissionResult(
            allowed=False,
            risk=Risk.CRITICAL,
            reason="Empty command",
        )

    if len(sub_results) == 1:
        return sub_results[0]

    # Collect suggested rules with a cap.
    all_rules: list[str] = []
    for r in sub_results:
        all_rules.extend(r.suggested_rules)
    if len(all_rules) > MAX_SUGGESTED_RULES_FOR_COMPOUND:
        all_rules = all_rules[:MAX_SUGGESTED_RULES_FOR_COMPOUND]

    # Deny if any deny.
    for r in sub_results:
        if not r.allowed and r.needs_user_decision:
            return PermissionResult(
                allowed=False,
                risk=Risk.HIGH,
                reason="Compound command contains denied subcommand(s)",
                suggested_rules=all_rules,
            )

    # Ask if any need asking.
    for r in sub_results:
        if r.needs_user_decision:
            return PermissionResult(
                allowed=False,
                risk=r.risk,
                reason="Compound command contains subcommand(s) requiring review",
                suggested_rules=all_rules,
            )

    # All safe → auto-approve.
    return PermissionResult(
        allowed=True,
        risk=Risk.SAFE,
        reason="All subcommands are safe",
        needs_user_decision=False,
    )


# ── Main permission check ─────────────────────────────────────────────

def analyze_command_permissions(command: str) -> PermissionResult:
    """Full multi-layer permission analysis for a shell command.

    This is the main entry point — it orchestrates all 5 steps.
    """
    # Step 1: Check if too many subcommands (CPU bomb protection).
    subcommands = _split_compound(command)
    if len(subcommands) > MAX_SUBCOMMANDS_FOR_SECURITY_CHECK:
        return PermissionResult(
            allowed=False,
            risk=Risk.HIGH,
            reason=f"Too many subcommands ({len(subcommands)} > {MAX_SUBCOMMANDS_FOR_SECURITY_CHECK})",
            needs_user_decision=True,
            parse_result=ParseResult.TOO_COMPLEX,
        )

    # Step 2: Analyze each subcommand.
    sub_results: list[PermissionResult] = []
    for sub in subcommands:
        sub_results.append(_analyze_single_command(sub))

    # Step 3: Aggregate.
    result = aggregate_compound_permissions(sub_results)

    # Step 4: Generate rule suggestions.
    if result.needs_user_decision:
        result.suggested_rules = _generate_rule_suggestions(subcommands)

    return result


def _analyze_single_command(command: str) -> PermissionResult:
    """Analyze a single (non-compound) command."""
    cleaned = command.strip()
    if not cleaned:
        return PermissionResult(
            allowed=True, risk=Risk.SAFE,
            reason="Empty command", needs_user_decision=False,
        )

    # Check dangerous patterns FIRST — before AST/security analysis.
    # This catches rm, sudo, mkfs, dd, chmod 777, etc.
    from ...security.patterns import is_dangerous as check_dangerous

    if check_dangerous(cleaned):
        return PermissionResult(
            allowed=False, risk=Risk.HIGH,
            reason="Command matches dangerous pattern",
            parse_result=ParseResult.SIMPLE,
        )

    # Try AST analysis.
    from .security import check_semantics, run_all_security_checks, is_blocking

    semantic = check_semantics(cleaned)
    if semantic == "dangerous":
        return PermissionResult(
            allowed=False, risk=Risk.HIGH,
            reason="Command contains dangerous patterns (AST)",
            parse_result=ParseResult.SIMPLE,
        )
    elif semantic == "too-complex":
        return PermissionResult(
            allowed=False, risk=Risk.HIGH,
            reason="Command is too complex for automated analysis",
            parse_result=ParseResult.TOO_COMPLEX,
        )

    # Run security checks.
    sec_result = run_all_security_checks(cleaned)
    if is_blocking(sec_result):
        findings_desc = "; ".join(desc for _, desc in sec_result.findings[:3])
        return PermissionResult(
            allowed=False, risk=Risk.CRITICAL,
            reason=f"Security check failed: {findings_desc}",
        )

    if sec_result.findings:
        findings_desc = "; ".join(desc for _, desc in sec_result.findings[:3])
        return PermissionResult(
            allowed=False, risk=Risk.NORMAL,
            reason=f"Security warnings: {findings_desc}",
        )

    return PermissionResult(
        allowed=True, risk=Risk.SAFE,
        reason="All checks passed",
        needs_user_decision=False,
        parse_result=ParseResult.SIMPLE,
    )


def _generate_rule_suggestions(
    subcommands: list[str],
) -> list[str]:
    """Generate permission rule suggestions for the user."""
    suggestions: list[str] = []
    seen: set[str] = set()

    for sub in subcommands:
        prefix = get_simple_command_prefix(sub)
        if prefix and prefix not in seen:
            seen.add(prefix)
            suggestions.append(f"Bash({prefix}:*)")
            if len(suggestions) >= MAX_SUGGESTED_RULES_FOR_COMPOUND:
                break

    if not suggestions:
        # Fallback: suggest exact command match.
        for sub in subcommands[:MAX_SUGGESTED_RULES_FOR_COMPOUND]:
            cleaned = sub.strip()
            if len(cleaned) > 80:
                cleaned = cleaned[:77] + "..."
            if cleaned not in seen:
                seen.add(cleaned)
                suggestions.append(f"Bash({cleaned})")

    return suggestions
