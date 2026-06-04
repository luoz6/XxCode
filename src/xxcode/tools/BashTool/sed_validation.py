"""sed command validation — whitelist-based security layer.

Prevents sed from being used as a backdoor to bypass FileEditTool
permissions.  sed can execute shell commands (e flag), write files
(w flag), and modify files in-place (-i) — all of which require
the same permission checks as direct file writes.

Strategy: whitelist — only known-safe patterns are auto-approved.
Everything else falls through to the interactive permission prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._tokenizer import tokenize


# ── sed flag analysis ─────────────────────────────────────────────────

# Safe flags for substitute commands.
# g = global, p = print, i/I = case-insensitive, m/M = multi-line
# Numbers 1-9 = which match to replace
_SAFE_SUBSTITUTE_FLAGS_RE = re.compile(r'^[gpiImM1-9]*$')

# sed commands that write to files.
_SED_WRITE_COMMANDS_RE = re.compile(r'[wW]')

# sed execute flag (runs shell commands).
_SED_EXECUTE_FLAGS_RE = re.compile(r'[eE]')


@dataclass
class SedValidationResult:
    """Result of sed command validation."""
    safe: bool = False
    reason: str = ""
    needs_file_permission: bool = False
    file_paths: list = field(default_factory=list)


def validate_sed_command(command: str) -> SedValidationResult:
    r"""Validate a sed command for security.

    This implements a whitelist approach — only known-safe patterns
    are automatically approved.  Everything else requires user review.

    Safe pattern 1: Pure line printing (requires -n flag)
      sed -n '5p'
      sed -n '1,10p'
      sed -n '1p;5p;10p'

    Safe pattern 2: Substitution expressions (no dangerous flags)
      sed 's/foo/bar/g'
      sed 's/foo/bar/gi'
      sed -i 's/foo/bar/' file.txt  (needs file permission for -i)

    Blocked:
      w/W flags  — file writes
      e/E flags  — command execution
      !          — address negation
      {} blocks  — sed script blocks
      Non-ASCII  — unicode homoglyph detection
      Backslash delimiters — parsing confusion (s\foo\bar\)
    """
    if not command.strip():
        return SedValidationResult(safe=False, reason="Empty command")

    # Extract the sed expression(s).
    expressions = _extract_sed_expressions(command)
    if not expressions:
        return SedValidationResult(safe=False, reason="Could not parse sed expression")

    has_i_flag = _has_inplace_flag(command)

    for expr in expressions:
        result = _validate_single_expression(expr, has_i_flag)
        if not result.safe:
            return result

    # All expressions are safe.
    file_paths = _extract_file_args(command) if has_i_flag else []

    return SedValidationResult(
        safe=True,
        reason="Safe sed operation",
        needs_file_permission=has_i_flag,
        file_paths=file_paths,
    )


def _extract_sed_expressions(command: str) -> list[str]:
    """Extract sed script expressions from the command line.

    Handles: -e 'script', -f scriptfile, inline 'script', and
    traditional sed 's/foo/bar/' file args.
    """
    expressions: list[str] = []
    tokens = _tokenize(command)
    i = 0

    while i < len(tokens):
        t = tokens[i]
        if t == "sed":
            i += 1
            continue
        if t in ("-e", "--expression"):
            if i + 1 < len(tokens):
                expressions.append(tokens[i + 1])
                i += 2
            else:
                i += 1
            continue
        if t in ("-f", "--file"):
            # Script file — can't validate content, reject.
            return []  # Force ask-user
        if t.startswith("-"):
            # Flag without argument.
            if t == "-i" or t.startswith("--in-place"):
                pass  # Not an expression.
            elif t == "-n" or t == "--quiet" or t == "--silent":
                pass  # Not an expression.
            else:
                # Unknown flag — conservative: ask user.
                pass
            i += 1
            continue
        # Non-flag, non-option token → could be the script.
        if _looks_like_sed_script(t):
            expressions.append(t)
        i += 1

    return expressions


def _validate_single_expression(
    expr: str, has_inplace: bool,
) -> SedValidationResult:
    """Validate a single sed expression/script."""
    if not expr:
        return SedValidationResult(safe=False, reason="Empty expression")

    # Block non-ASCII characters (homoglyph detection).
    if not all(ord(c) < 128 for c in expr):
        return SedValidationResult(
            safe=False,
            reason="Expression contains non-ASCII characters",
        )

    # Block backslash delimiters (parsing confusion).
    if expr.startswith("s\\"):
        return SedValidationResult(
            safe=False,
            reason="Backslash delimiter in substitute command",
        )

    # Block sed script blocks {}.
    if "{" in expr or "}" in expr:
        return SedValidationResult(
            safe=False,
            reason="Script blocks {} are not auto-approved",
        )

    # Block address negation ! — matches /pattern/!cmd, 5!cmd, or !cmd forms.
    if "!" in expr and not expr.startswith("#!"):
        if re.search(r'(?:/(?:.*)|\d)! *[a-zA-Z]', expr) or re.match(r'^!\s*[a-zA-Z]', expr.strip()):
            return SedValidationResult(
                safe=False,
                reason="Address negation (!) is not auto-approved",
            )

    # Check for dangerous execute flags.
    if _SED_EXECUTE_FLAGS_RE.search(expr):
        return SedValidationResult(
            safe=False,
            reason="Execute flag (e/E) can run arbitrary shell commands",
        )

    # Check for dangerous write flags.
    if _SED_WRITE_COMMANDS_RE.search(expr):
        return SedValidationResult(
            safe=False,
            reason="Write command (w/W) can write to arbitrary files",
        )

    # Check for delete/append/insert/change commands.
    # These modify the pattern space and could be used with -i.
    if has_inplace:
        dangerous_inplace = re.search(r'(?:^|[^a-zA-Z])([daic])\s', expr)
        if dangerous_inplace:
            return SedValidationResult(
                safe=False,
                reason=f"'{dangerous_inplace.group(1)}' command with -i modifies files",
            )

    # ── Whitelist check ──────────────────────────────────────────

    # Pattern 1: Pure line printing (p command).
    # Note: ! is deliberately excluded — address negation is blocked above.
    if re.match(r'^[\d,\s;$]*p[\d;]*$', expr.strip()):
        return SedValidationResult(
            safe=True,
            reason="Safe: line printing only (p command)",
        )

    # Pattern 2: Substitution s/pattern/replacement/flags.
    sub_match = re.match(
        r'^s([^a-zA-Z0-9\s])(.*)\1(.*)\1([gpiImM1-9]*)$', expr.strip(),
    )
    if sub_match:
        flags = sub_match.group(4) if sub_match.lastindex and sub_match.lastindex >= 4 else ""
        if not flags or _SAFE_SUBSTITUTE_FLAGS_RE.match(flags):
            return SedValidationResult(
                safe=True,
                reason="Safe: substitution with allowed flags",
            )

    # Pattern 3: Multiple expressions separated by ; or newline.
    # Recurse into each sub-expression.
    if ";" in expr:
        parts = _split_sed_expressions(expr)
        if len(parts) > 1:
            for part in parts:
                result = _validate_single_expression(part.strip(), has_inplace)
                if not result.safe:
                    return result
            return SedValidationResult(
                safe=True,
                reason="Safe: all sub-expressions passed",
            )

    return SedValidationResult(
        safe=False,
        reason="Expression does not match any safe pattern",
    )


def _looks_like_sed_script(token: str) -> bool:
    """Heuristic: does this token look like a sed script?"""
    # Starts with s/, /pattern/, or a number followed by command.
    if re.match(r'^[sdyapicq]', token):
        return True
    if re.match(r'^[\d,$]', token):
        return True
    if re.match(r'^/', token):
        return True
    return False


def _split_sed_expressions(expr: str) -> list[str]:
    """Split sed expressions on ; while respecting quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    for ch in expr:
        if ch == "'":
            in_single = not in_single
            current.append(ch)
        elif ch == ";" and not in_single:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _has_inplace_flag(command: str) -> bool:
    """Check if sed command uses -i (in-place edit) flag."""
    tokens = _tokenize(command)
    for t in tokens:
        if t == "-i" or t.startswith("--in-place"):
            return True
        # sed -i.bak style
        if re.match(r'^-i\.', t):
            return True
    return False


def _extract_file_args(command: str) -> list[str]:
    """Extract file arguments from a sed command."""
    tokens = _tokenize(command)
    files: list[str] = []
    past_script = False
    for t in tokens:
        if t == "sed":
            continue
        if t in ("-i", "--in-place") or re.match(r'^-i\.', t):
            continue
        if t in ("-n", "--quiet", "--silent", "-e", "--expression", "-f", "--file"):
            continue
        if t.startswith("-") and len(t) > 1 and t[1] != "-":
            # Combined short flags like -in, -ie
            continue
        if _looks_like_sed_script(t):
            past_script = True
            continue
        if past_script:
            files.append(t)
    return files


def _tokenize(command: str) -> list[str]:
    """Tokenize a sed command — delegates to shared tokenizer with strip_quotes=True."""
    return tokenize(command, strip_quotes=True)
