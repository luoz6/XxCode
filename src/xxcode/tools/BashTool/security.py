"""Bash command security validation — 23 security checks.

Port of Claude Code's bashSecurity.ts (~800 lines).  This is the first
defence line for shell commands, executing BEFORE the permission check.

Architecture:
  1. Quoted content extraction (3 variants)
  2. Command substitution blocking (11 patterns)
  3. Zsh-specific dangerous command detection (18 commands)
  4. Obfuscation detection (flags, metacharacters, variables)
  5. Structural attack detection (comment-quote desync, backslash operators)
  6. Character-level attacks (control chars, unicode whitespace, newlines)
  7. Tree-sitter AST semantic check (with regex fallback)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


# ── Security check ID mapping ─────────────────────────────────────────
# Numeric IDs avoid logging mutable strings in telemetry.

class SecurityCheckId(IntEnum):
    INCOMPLETE_COMMANDS = 1
    JQ_SYSTEM_FUNCTION = 2
    JQ_FILE_ARGUMENTS = 3
    OBFUSCATED_FLAGS = 4
    SHELL_METACHARACTERS = 5
    DANGEROUS_VARIABLES = 6
    NEWLINES = 7
    DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION = 8
    DANGEROUS_PATTERNS_INPUT_REDIRECTION = 9
    DANGEROUS_PATTERNS_OUTPUT_REDIRECTION = 10
    IFS_INJECTION = 11
    GIT_COMMIT_SUBSTITUTION = 12
    PROC_ENVIRON_ACCESS = 13
    MALFORMED_TOKEN_INJECTION = 14
    BACKSLASH_ESCAPED_WHITESPACE = 15
    BRACE_EXPANSION = 16
    CONTROL_CHARACTERS = 17
    UNICODE_WHITESPACE = 18
    MID_WORD_HASH = 19
    ZSH_DANGEROUS_COMMANDS = 20
    BACKSLASH_ESCAPED_OPERATORS = 21
    COMMENT_QUOTE_DESYNC = 22
    QUOTED_NEWLINE = 23


BASH_SECURITY_CHECK_IDS: dict[str, int] = {
    "INCOMPLETE_COMMANDS": 1,
    "JQ_SYSTEM_FUNCTION": 2,
    "JQ_FILE_ARGUMENTS": 3,
    "OBFUSCATED_FLAGS": 4,
    "SHELL_METACHARACTERS": 5,
    "DANGEROUS_VARIABLES": 6,
    "NEWLINES": 7,
    "DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION": 8,
    "DANGEROUS_PATTERNS_INPUT_REDIRECTION": 9,
    "DANGEROUS_PATTERNS_OUTPUT_REDIRECTION": 10,
    "IFS_INJECTION": 11,
    "GIT_COMMIT_SUBSTITUTION": 12,
    "PROC_ENVIRON_ACCESS": 13,
    "MALFORMED_TOKEN_INJECTION": 14,
    "BACKSLASH_ESCAPED_WHITESPACE": 15,
    "BRACE_EXPANSION": 16,
    "CONTROL_CHARACTERS": 17,
    "UNICODE_WHITESPACE": 18,
    "MID_WORD_HASH": 19,
    "ZSH_DANGEROUS_COMMANDS": 20,
    "BACKSLASH_ESCAPED_OPERATORS": 21,
    "COMMENT_QUOTE_DESYNC": 22,
    "QUOTED_NEWLINE": 23,
}


# ── Quoted content extraction ─────────────────────────────────────────
#
# Before running security checks, we extract quoted content to avoid
# false positives on string literals like echo '$(...)'.
# Three variants are produced for different check types.

@dataclass
class QuotedExtraction:
    """Result of extracting quoted content from a command string."""
    with_double_quotes: str = ""
    fully_unquoted: str = ""
    unquoted_keep_quote_chars: str = ""


def extract_quoted_content(command: str) -> QuotedExtraction:
    """Strip quoted content from a command, producing 3 variants.

    with_double_quotes:     Only single-quoted content stripped.
    fully_unquoted:         All quoted content stripped (single + double).
    unquoted_keep_quote_chars: Content stripped but quote chars preserved
                               (used for detecting quote-adjacency attacks).
    """
    with_dq: list[str] = []
    fully: list[str] = []
    keep_chars: list[str] = []
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "'":
            # Single-quoted: skip content, keep nothing.
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1  # Skip closing quote
        elif ch == '"':
            # Double-quoted: keep for with_dq, skip for fully.
            keep_chars.append(ch)
            with_dq.append(ch)
            i += 1
            while i < n and command[i] != '"':
                if command[i] == '\\' and i + 1 < n:
                    keep_chars.append(command[i])
                    with_dq.append(command[i])
                    i += 1
                    keep_chars.append(command[i])
                    with_dq.append(command[i])
                else:
                    keep_chars.append(command[i])
                    with_dq.append(command[i])
                i += 1
            if i < n:
                keep_chars.append(command[i])
                with_dq.append(command[i])
                i += 1  # Skip closing quote
        elif ch == '\\' and i + 1 < n:
            # Backslash escape: keep escaped char, skip the backslash
            # in the fully_unquoted variant.
            keep_chars.append(ch)
            keep_chars.append(command[i + 1])
            with_dq.append(ch)
            with_dq.append(command[i + 1])
            fully.append(command[i + 1])
            i += 2
        else:
            keep_chars.append(ch)
            with_dq.append(ch)
            fully.append(ch)
            i += 1

    return QuotedExtraction(
        with_double_quotes="".join(with_dq),
        fully_unquoted="".join(fully),
        unquoted_keep_quote_chars="".join(keep_chars),
    )


# ── Command substitution patterns ─────────────────────────────────────
#
# 11 patterns covering all known command substitution forms.
# These are checked on fully_unquoted content to avoid false positives
# on string literals like echo '$(whoami)'.

COMMAND_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'<\('), 'process substitution <()'),
    (re.compile(r'>\('), 'process substitution >()'),
    (re.compile(r'=\('), 'Zsh process substitution =()'),
    # Zsh EQUALS expansion: =cmd expands to full path.
    # =curl evil.com bypasses Bash(curl:*) deny rules because
    # the parser sees the base command as =curl not curl.
    (re.compile(r'(?:^|[\s;&|])=[a-zA-Z_]'), 'Zsh equals expansion (=cmd)'),
    (re.compile(r'\$\('), '$() command substitution'),
    (re.compile(r'\$\{'), '${} parameter substitution'),
    (re.compile(r'\$\['), '$[] legacy arithmetic expansion'),
    (re.compile(r'~\['), 'Zsh-style parameter expansion'),
    (re.compile(r'\(e:'), 'Zsh-style glob qualifiers'),
    (re.compile(r'\(\+'), 'Zsh glob qualifier with command execution'),
    (re.compile(r'\}\s*always\s*\{'), 'Zsh always block (try/always construct)'),
    (re.compile(r'<#'), 'PowerShell comment syntax (defence in depth)'),
]


def check_command_substitution(command: str) -> list[tuple[int, str]]:
    r"""Check for command substitution patterns in unquoted content.

    Uses with_double_quotes (NOT fully_unquoted) because command
    substitution inside double quotes IS executed by bash:
      echo \"\$(whoami)\"   → code executes
      echo '$(whoami)'    → single-quoted, safe (stripped from with_dq)

    Returns list of (check_id, description) for each match found.
    """
    extraction = extract_quoted_content(command)
    target = extraction.with_double_quotes
    findings: list[tuple[int, str]] = []
    for pattern, desc in COMMAND_SUBSTITUTION_PATTERNS:
        m = pattern.search(target)
        if m:
            findings.append((
                BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION"],
                f"{desc}: matched '{m.group()}'",
            ))
    return findings


# ── Zsh dangerous commands ────────────────────────────────────────────
#
# 18 Zsh builtins that can execute code or manipulate files outside
# normal permission checks.

ZSH_DANGEROUS_COMMANDS: set[str] = {
    # Module loader — entry point for zsh/mapfile, zsh/zpty, zsh/net/tcp, zsh/files
    "zmodload",
    # Equivalent to eval — executes arbitrary code (emulate -c)
    "emulate",
    # zsh/system — fine-grained file descriptor operations
    "sysopen", "sysread", "syswrite", "sysseek",
    # zsh/files — built-in file operations that bypass binary checks
    "zf_rm", "zf_mv", "zf_ln", "zf_chmod", "zf_chown",
    "zf_mkdir", "zf_rmdir", "zf_symlink",
}


def check_zsh_dangerous(command: str) -> list[tuple[int, str]]:
    """Check for Zsh dangerous builtins in the command."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    findings: list[tuple[int, str]] = []
    tokens = target.split()
    for token in tokens:
        base = token.split("/")[-1]  # Strip path prefix
        if base in ZSH_DANGEROUS_COMMANDS:
            findings.append((
                BASH_SECURITY_CHECK_IDS["ZSH_DANGEROUS_COMMANDS"],
                f"Zsh dangerous command: {base}",
            ))
    return findings


# ── Obfuscation detection ─────────────────────────────────────────────

# Flag obfuscation: detecting flags hidden through concatenation
# e.g. -`echo e`v`echo il` → -evil
_OBFUSCATED_FLAG_RE = re.compile(
    r'(?:^|[\s;&|])-[a-zA-Z]*[`$]',
)

# Shell metacharacters in unquoted content (beyond normal usage).
_SHELL_METACHAR_RE = re.compile(
    r'[;&|`]',
)

# Dangerous variable expansions in pipes/redirects.
_DANGEROUS_VARIABLES_RE = re.compile(
    r'(?:^|\s)(?:TF|VAR|CMD|EXEC|EVAL|PAYLOAD|SHELL|BASH|EXPLOIT)\s*=',
    re.IGNORECASE,
)


def check_obfuscated_flags(command: str) -> list[tuple[int, str]]:
    """Detect obfuscated command flags via command substitution."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _OBFUSCATED_FLAG_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"],
                 "Obfuscated flag detected: command substitution in flag")]
    return []


def check_shell_metacharacters(command: str) -> list[tuple[int, str]]:
    """Check for shell metacharacters used for command chaining."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    findings: list[tuple[int, str]] = []
    # Only flag if metacharacters appear in suspicious combinations
    # (standalone ; or | are common in valid commands)
    if target.count("`") >= 2:
        findings.append((
            BASH_SECURITY_CHECK_IDS["SHELL_METACHARACTERS"],
            "Backtick command substitution detected",
        ))
    return findings


def check_dangerous_variables(command: str) -> list[tuple[int, str]]:
    """Detect suspicious variable assignments."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _DANGEROUS_VARIABLES_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["DANGEROUS_VARIABLES"],
                 "Suspicious variable assignment detected")]
    return []


# ── Newline injection ─────────────────────────────────────────────────

def check_newlines(command: str) -> list[tuple[int, str]]:
    """Check for unquoted newlines that could inject commands."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if "\n" in target or "\r" in target:
        return [(BASH_SECURITY_CHECK_IDS["NEWLINES"],
                 "Unquoted newline/CR in command")]
    return []


# ── IFS injection ─────────────────────────────────────────────────────

_IFS_INJECTION_RE = re.compile(r'IFS\s*=')


def check_ifs_injection(command: str) -> list[tuple[int, str]]:
    """Detect IFS variable manipulation for argument splitting attacks."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _IFS_INJECTION_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["IFS_INJECTION"],
                 "IFS variable manipulation detected")]
    return []


# ── Git commit substitution ───────────────────────────────────────────

_GIT_COMMIT_SUB_RE = re.compile(r'git\s+commit.*\$\(', re.IGNORECASE)


def check_git_commit_substitution(command: str) -> list[tuple[int, str]]:
    """Detect command substitution in git commit messages."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _GIT_COMMIT_SUB_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["GIT_COMMIT_SUBSTITUTION"],
                 "Command substitution in git commit")]
    return []


# ── /proc/self/environ access ────────────────────────────────────────

_PROC_ENVIRON_RE = re.compile(r'/proc/(?:self|\d+)/environ')


def check_proc_environ_access(command: str) -> list[tuple[int, str]]:
    """Detect access to /proc/self/environ for credential theft."""
    if _PROC_ENVIRON_RE.search(command):
        return [(BASH_SECURITY_CHECK_IDS["PROC_ENVIRON_ACCESS"],
                 "/proc/self/environ access detected")]
    return []


# ── Malformed token injection ─────────────────────────────────────────

_MALFORMED_TOKEN_RE = re.compile(r'[A-Za-z0-9+/=]{40,}')


def check_malformed_token(command: str) -> list[tuple[int, str]]:
    """Detect base64-encoded blobs that may be injected payloads."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _MALFORMED_TOKEN_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["MALFORMED_TOKEN_INJECTION"],
                 "Base64-encoded blob detected")]
    return []


# ── Backslash-escaped whitespace ──────────────────────────────────────

_BACKSLASH_WHITESPACE_RE = re.compile(r'\\[ \t]')


def check_backslash_escaped_whitespace(command: str) -> list[tuple[int, str]]:
    """Detect backslash-escaped spaces used to hide arguments.

    Must use with_double_quotes (not fully_unquoted) — the latter
    strips backslash escapes, making detection impossible.
    """
    extraction = extract_quoted_content(command)
    target = extraction.with_double_quotes
    if _BACKSLASH_WHITESPACE_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_WHITESPACE"],
                 "Backslash-escaped whitespace detected")]
    return []


# ── Brace expansion ───────────────────────────────────────────────────

_BRACE_EXPANSION_RE = re.compile(r'\{[a-zA-Z0-9_.,]+\.\.[a-zA-Z0-9_.,]+\}')


def check_brace_expansion(command: str) -> list[tuple[int, str]]:
    """Detect brace expansion that may hide malicious arguments."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _BRACE_EXPANSION_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["BRACE_EXPANSION"],
                 "Brace expansion detected")]
    return []


# ── Control characters ────────────────────────────────────────────────

_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def check_control_characters(command: str) -> list[tuple[int, str]]:
    """Detect non-printable control characters (excluding tab, newline, CR)."""
    if _CONTROL_CHARS_RE.search(command):
        return [(BASH_SECURITY_CHECK_IDS["CONTROL_CHARACTERS"],
                 "Control characters detected")]
    return []


# ── Unicode whitespace homoglyphs ─────────────────────────────────────

# Unicode whitespace characters that look like regular spaces.
_UNICODE_WHITESPACE_CHARS = (
    ' '  # NO-BREAK SPACE
    ' '  # OGHAM SPACE MARK
    '᠎'  # MONGOLIAN VOWEL SEPARATOR
    ' '  # EN QUAD
    ' '  # EM QUAD
    ' '  # EN SPACE
    ' '  # EM SPACE
    ' '  # THREE-PER-EM SPACE
    ' '  # FOUR-PER-EM SPACE
    ' '  # SIX-PER-EM SPACE
    ' '  # FIGURE SPACE
    ' '  # PUNCTUATION SPACE
    ' '  # THIN SPACE
    ' '  # HAIR SPACE
    ' '  # LINE SEPARATOR
    ' '  # PARAGRAPH SEPARATOR
    ' '  # NARROW NO-BREAK SPACE
    ' '  # MEDIUM MATHEMATICAL SPACE
    '　'  # IDEOGRAPHIC SPACE
    '﻿'  # ZERO WIDTH NO-BREAK SPACE (BOM)
)

_UNICODE_WHITESPACE_RE = re.compile(f'[{_UNICODE_WHITESPACE_CHARS}]')


def check_unicode_whitespace(command: str) -> list[tuple[int, str]]:
    """Detect Unicode whitespace homoglyphs used to hide commands."""
    if _UNICODE_WHITESPACE_RE.search(command):
        return [(BASH_SECURITY_CHECK_IDS["UNICODE_WHITESPACE"],
                 "Unicode whitespace homoglyph detected")]
    return []


# ── Mid-word hash comment ─────────────────────────────────────────────

_MID_WORD_HASH_RE = re.compile(r'[a-zA-Z_]#[a-zA-Z_]')


def check_mid_word_hash(command: str) -> list[tuple[int, str]]:
    """Detect mid-word # that could be a comment insertion attack."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _MID_WORD_HASH_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["MID_WORD_HASH"],
                 "Mid-word # detected (possible comment injection)")]
    return []


# ── Comment-quote desync ──────────────────────────────────────────────

def check_comment_quote_desync(command: str) -> list[tuple[int, str]]:
    """Detect comment-quote desynchronisation attacks.

    An attacker can craft input like:  cmd 'arg1 #' arg2
    where the # inside quotes is harmless during normal execution,
    but after naive quote-stripping becomes: cmd  arg2
    — a completely different command.
    """
    # If the command has quotes, scan for # inside quoted content
    # that could become a comment after naive quote stripping.
    if "'" in command or '"' in command:
        # Check if there's a # inside quoted content that would
        # become a real comment after stripping.
        in_single = False
        in_double = False
        i = 0
        while i < len(command):
            ch = command[i]
            if ch == "'" and not in_double:
                in_single = not in_single
                # Check if # follows inside the quotes
                j = i + 1
                while j < len(command) and command[j] != "'":
                    if command[j] == '#':
                        return [(BASH_SECURITY_CHECK_IDS["COMMENT_QUOTE_DESYNC"],
                                 "Comment-quote desync: # inside single quotes")]
                    j += 1
            elif ch == '"' and not in_single:
                in_double = not in_double
            i += 1
    return []


# ── Backslash-escaped operators ───────────────────────────────────────

_BACKSLASH_OPERATOR_RE = re.compile(r'\\(?:&&|\|\||;|\||&|>|<)')


def check_backslash_escaped_operators(command: str) -> list[tuple[int, str]]:
    """Detect backslash-escaped shell operators.

    Must check the with_double_quotes variant (not fully_unquoted),
    because fully_unquoted strips backslashes — losing the very
    escape sequences we're trying to detect.
    """
    extraction = extract_quoted_content(command)
    target = extraction.with_double_quotes  # Preserves backslash sequences
    if _BACKSLASH_OPERATOR_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_OPERATORS"],
                 "Backslash-escaped shell operator detected")]
    return []


# ── Quoted newline ────────────────────────────────────────────────────

def check_quoted_newline(command: str) -> list[tuple[int, str]]:
    """Detect newlines inside quotes that may hide command injection."""
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in ("\n", "\r") and (in_single or in_double):
            return [(BASH_SECURITY_CHECK_IDS["QUOTED_NEWLINE"],
                     "Newline inside quoted string")]
        i += 1
    return []


# ── JQ-specific checks ────────────────────────────────────────────────

_JQ_SYSTEM_RE = re.compile(r'\bjq\b.*system\s*\(')
_JQ_FILE_ARG_RE = re.compile(r'\bjq\b.*(?:--arg|--argjson|--rawfile)')


def check_jq_system(command: str) -> list[tuple[int, str]]:
    """Detect jq system() function calls for code execution."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _JQ_SYSTEM_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["JQ_SYSTEM_FUNCTION"],
                 "jq system() function call detected")]
    return []


def check_jq_file_args(command: str) -> list[tuple[int, str]]:
    """Detect jq file argument flags that may leak file contents."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _JQ_FILE_ARG_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["JQ_FILE_ARGUMENTS"],
                 "jq file argument flags detected")]
    return []


# ── Incomplete commands ───────────────────────────────────────────────

_INCOMPLETE_PATTERNS = [
    (re.compile(r'\|\s*$'), 'Trailing pipe'),
    (re.compile(r'&&\s*$'), 'Trailing &&'),
    (re.compile(r'\|\|\s*$'), 'Trailing ||'),
    (re.compile(r';\s*$'), 'Trailing semicolon'),
    (re.compile(r'\\\s*$'), 'Trailing backslash (line continuation)'),
]


def check_incomplete_commands(command: str) -> list[tuple[int, str]]:
    """Detect incomplete command fragments."""
    findings: list[tuple[int, str]] = []
    for pattern, desc in _INCOMPLETE_PATTERNS:
        if pattern.search(command.strip()):
            findings.append((
                BASH_SECURITY_CHECK_IDS["INCOMPLETE_COMMANDS"],
                desc,
            ))
    return findings


# ── Input/output redirection checks ───────────────────────────────────

_DANGEROUS_INPUT_REDIR_RE = re.compile(r'<\s*/dev/(?:tcp|udp)')
_DANGEROUS_OUTPUT_REDIR_RE = re.compile(r'>\s*(?:/dev/[hs]d|/proc/)')


def check_input_redirection(command: str) -> list[tuple[int, str]]:
    """Detect dangerous input redirection."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _DANGEROUS_INPUT_REDIR_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_INPUT_REDIRECTION"],
                 "Dangerous input redirection to /dev/tcp or /dev/udp")]
    return []


def check_output_redirection(command: str) -> list[tuple[int, str]]:
    """Detect dangerous output redirection to device files."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted
    if _DANGEROUS_OUTPUT_REDIR_RE.search(target):
        return [(BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_OUTPUT_REDIRECTION"],
                 "Dangerous output redirection to device/proc files")]
    return []


# ── Orchestrator ──────────────────────────────────────────────────────

@dataclass
class SecurityCheckResult:
    """Aggregate result of all security checks on a command."""
    passed: bool = True
    findings: list[tuple[int, str]] = field(default_factory=list)
    check_ids: set[int] = field(default_factory=set)


def run_all_security_checks(command: str) -> SecurityCheckResult:
    """Run all 23 security checks against a command.

    Args:
        command: Raw shell command string from the model.

    Returns:
        SecurityCheckResult with aggregated findings.
    """
    result = SecurityCheckResult()

    checks = [
        check_incomplete_commands,
        check_jq_system,
        check_jq_file_args,
        check_obfuscated_flags,
        check_shell_metacharacters,
        check_dangerous_variables,
        check_newlines,
        check_command_substitution,
        check_input_redirection,
        check_output_redirection,
        check_ifs_injection,
        check_git_commit_substitution,
        check_proc_environ_access,
        check_malformed_token,
        check_backslash_escaped_whitespace,
        check_brace_expansion,
        check_control_characters,
        check_unicode_whitespace,
        check_mid_word_hash,
        check_zsh_dangerous,
        check_backslash_escaped_operators,
        check_comment_quote_desync,
        check_quoted_newline,
    ]

    for check_fn in checks:
        findings = check_fn(command)
        for check_id, desc in findings:
            result.findings.append((check_id, desc))
            result.check_ids.add(check_id)

    result.passed = len(result.findings) == 0
    return result


# ── Severity classification ───────────────────────────────────────────

# Security checks that always block execution regardless of context.
BLOCKING_CHECK_IDS: set[int] = {
    BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION"],
    BASH_SECURITY_CHECK_IDS["ZSH_DANGEROUS_COMMANDS"],
    BASH_SECURITY_CHECK_IDS["PROC_ENVIRON_ACCESS"],
    BASH_SECURITY_CHECK_IDS["CONTROL_CHARACTERS"],
    BASH_SECURITY_CHECK_IDS["NEWLINES"],
    BASH_SECURITY_CHECK_IDS["IFS_INJECTION"],
}

# Security checks that trigger a user prompt but can be overridden.
WARNING_CHECK_IDS: set[int] = {
    BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"],
    BASH_SECURITY_CHECK_IDS["SHELL_METACHARACTERS"],
    BASH_SECURITY_CHECK_IDS["DANGEROUS_VARIABLES"],
    BASH_SECURITY_CHECK_IDS["MALFORMED_TOKEN_INJECTION"],
    BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_WHITESPACE"],
    BASH_SECURITY_CHECK_IDS["BRACE_EXPANSION"],
    BASH_SECURITY_CHECK_IDS["UNICODE_WHITESPACE"],
    BASH_SECURITY_CHECK_IDS["MID_WORD_HASH"],
    BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_OPERATORS"],
    BASH_SECURITY_CHECK_IDS["COMMENT_QUOTE_DESYNC"],
    BASH_SECURITY_CHECK_IDS["QUOTED_NEWLINE"],
    BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_INPUT_REDIRECTION"],
    BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_OUTPUT_REDIRECTION"],
    BASH_SECURITY_CHECK_IDS["GIT_COMMIT_SUBSTITUTION"],
    BASH_SECURITY_CHECK_IDS["INCOMPLETE_COMMANDS"],
    BASH_SECURITY_CHECK_IDS["JQ_SYSTEM_FUNCTION"],
    BASH_SECURITY_CHECK_IDS["JQ_FILE_ARGUMENTS"],
}


def is_blocking(result: SecurityCheckResult) -> bool:
    """Check if any finding is a blocking-level issue."""
    return bool(result.check_ids & BLOCKING_CHECK_IDS)


def is_warning_only(result: SecurityCheckResult) -> bool:
    """Check if all findings are warning-level only (not blocking)."""
    if not result.findings:
        return False
    return not bool(result.check_ids & BLOCKING_CHECK_IDS)


# ── Tree-sitter / AST semantic check (fallback) ───────────────────────

def check_semantics(command: str) -> str:
    """Semantic safety check using AST/regex analysis.

    Returns:
        'safe'        — command appears safe
        'dangerous'   — command contains suspicious patterns
        'too-complex' — command is too complex to analyze safely

    When tree-sitter-bash is unavailable, this falls back to regex-based
    analysis that checks for command structure complexity indicators.
    """
    # Try tree-sitter first.
    try:
        result = _check_semantics_ast(command)
        if result:
            return result
    except ImportError:
        pass

    # Fall back to regex-based semantic analysis.
    return _check_semantics_regex(command)


def _check_semantics_ast(command: str) -> str | None:
    """AST-based semantic check (requires tree-sitter-bash)."""
    try:
        import tree_sitter_bash as tsb
    except ImportError:
        return None

    try:
        parser = tsb.parser()
        tree = parser.parse(command.encode())
        root = tree.root_node

        # Walk AST looking for dangerous constructs.
        has_command_sub = False
        has_redirect = False
        has_process_sub = False

        def walk(node):
            nonlocal has_command_sub, has_redirect, has_process_sub
            node_type = node.type if hasattr(node, 'type') else str(node)
            if node_type in ("command_substitution",):
                has_command_sub = True
            elif node_type in ("file_redirect",):
                has_redirect = True
            elif node_type in ("process_substitution",):
                has_process_sub = True
            for child in node.children if hasattr(node, 'children') else []:
                walk(child)

        walk(root)

        if has_process_sub:
            return "dangerous"
        if has_command_sub and has_redirect:
            return "too-complex"
        return "safe"
    except Exception:
        return "too-complex"


def _check_semantics_regex(command: str) -> str:
    """Regex-based semantic fallback for safety classification."""
    extraction = extract_quoted_content(command)
    target = extraction.fully_unquoted

    # Check for process substitution (always dangerous).
    for pattern, _ in COMMAND_SUBSTITUTION_PATTERNS[:3]:  # <() >() =()
        if pattern.search(target):
            return "dangerous"

    # Check for complexity indicators.
    complexity_indicators = [
        r'`[^`]+`',         # Backtick command substitution
        r'\$\([^)]+\)',     # $() command substitution
        r'\|.*\|',          # Multiple pipes
        r'&&.*&&',          # Multiple AND chains
        r'\|\|.*\|\|',      # Multiple OR chains
    ]

    complexity_score = 0
    for indicator in complexity_indicators:
        if re.search(indicator, target):
            complexity_score += 1

    if complexity_score >= 3:
        return "too-complex"
    if complexity_score >= 1:
        return "dangerous"

    return "safe"
