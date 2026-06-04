"""Canonical shell tokenizer and command splitting helpers for BashTool."""

from __future__ import annotations

import re


SAFE_ENV_VARS: set[str] = {
    "GOEXPERIMENT",
    "GOOS",
    "GOARCH",
    "GOPATH",
    "GOROOT",
    "GOPROXY",
    "GOMODCACHE",
    "GONOSUMCHECK",
    "GONOSUMDB",
    "GOPRIVATE",
    "RUST_BACKTRACE",
    "RUST_LOG",
    "RUSTFLAGS",
    "NODE_ENV",
    "NODE_OPTIONS",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHONWARNINGS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_TIME",
    "HOME",
    "USER",
    "PATH",
    "TERM",
    "SHELL",
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "EDITOR",
    "VISUAL",
    "PAGER",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")


def tokenize(command: str, *, strip_quotes: bool = False) -> list[str]:
    """Tokenize a shell command while respecting quotes and backslash escapes."""
    tokens: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            current.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            if not strip_quotes:
                current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            if not strip_quotes:
                current.append(ch)
        elif ch in (" ", "\t") and not in_single and not in_double:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1

    if current:
        tokens.append("".join(current))

    return tokens


def split_pipeline(command: str) -> list[str]:
    """Split a compound command by shell control operators while respecting quotes."""
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            two_char = command[i : i + 2]
            if two_char in ("&&", "||"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue
            if ch in (";", "|"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue
            if ch == "&":
                prev = "".join(current).rstrip()
                if prev and prev[-1] in (">", "<"):
                    current.append(ch)
                    i += 1
                    continue
                if i + 1 < n and command[i + 1] == ">":
                    current.append(ch)
                    i += 1
                    continue
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1

    remaining = "".join(current).strip()
    if remaining:
        segments.append(remaining)

    return segments if segments else [command]


def _split_first_shell_token(command: str) -> tuple[str, str] | None:
    cleaned = command.lstrip()
    if not cleaned:
        return None

    in_single = False
    in_double = False
    i = 0
    n = len(cleaned)

    while i < n:
        ch = cleaned[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in (" ", "\t") and not in_single and not in_double:
            break
        i += 1

    return cleaned[:i], cleaned[i:]


def _leading_env_name(token: str) -> str | None:
    if "=" not in token:
        return None
    name, _value = token.split("=", 1)
    if not _ENV_NAME_RE.fullmatch(name):
        return None
    return name


def strip_safe_env_vars(command: str) -> str:
    """Strip one leading safe env assignment when followed by a command."""
    cleaned = command.lstrip()
    if not cleaned:
        return ""

    split = _split_first_shell_token(cleaned)
    if split is None:
        return cleaned

    token, rest = split
    env_name = _leading_env_name(token)
    if env_name is None or env_name not in SAFE_ENV_VARS:
        return cleaned

    if not rest.strip():
        return cleaned

    return rest.lstrip()


def strip_all_safe_env_prefixes(command: str) -> str:
    """Repeatedly strip leading safe env assignments."""
    previous = None
    current = command
    while previous != current:
        previous = current
        current = strip_safe_env_vars(current)
    return current


def normalize_base_token(token: str) -> str:
    """Normalize a base token by removing path prefixes and extensions."""
    if "=" in token:
        return token

    token = token.rsplit("/", 1)[-1]
    token = token.rsplit("\\", 1)[-1]

    if "." in token:
        name_part = token.rsplit(".", 1)[0]
        if name_part:
            token = name_part

    return token


def extract_base_command(command: str) -> str | None:
    """Extract the normalized base command name from a command line."""
    cleaned = strip_all_safe_env_prefixes(command.strip())
    if not cleaned:
        return None

    split = _split_first_shell_token(cleaned)
    if split is None:
        return None
    base, rest = split

    if base in ("sudo", "doas", "pkexec"):
        next_split = _split_first_shell_token(rest)
        if next_split is None:
            return None
        base, _rest = next_split

    return normalize_base_token(base)
