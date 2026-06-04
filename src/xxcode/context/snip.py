"""L1: History Snip — regex-based trimming of stale tool outputs.

Runs on every compression pass (lowest cost). Removes noise lines from
tool_result blocks so the model doesn't waste context on irrelevant output.
"""

import copy
import re
from typing import Any

from .tokens import rough_estimate

# ── Snip patterns ───────────────────────────────────────────────────
# (compiled regex, description, replacement string)
# Matched lines are replaced (empty replacement = line removal).

_SNIP_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # pip install progress
    (re.compile(r"^\s*Collecting\s+\S+", re.MULTILINE), "pip collecting", ""),
    (re.compile(r"^\s*Downloading\s+\S+", re.MULTILINE), "pip downloading", ""),
    (re.compile(r"^\s*Requirement already satisfied:.*", re.MULTILINE), "pip satisfied", ""),
    (re.compile(r"^\s*Installing collected packages:.*", re.MULTILINE), "pip installing", ""),
    (
        re.compile(r"^Successfully installed[\s\S]+?(?=\n\n|\Z)", re.MULTILINE),
        "pip success block",
        "[pip packages installed]",
    ),

    # npm / yarn
    (re.compile(r"^\s*npm\s+(install|run|build).*", re.MULTILINE), "npm cmd", ""),
    (re.compile(r"^added \d+ packages.*", re.MULTILINE), "npm added", ""),

    # Progress bars and counters
    (re.compile(r"^\[\d+/\d+\].*(?:completed|done).*", re.MULTILINE), "progress", ""),
    (re.compile(r"^\s*\d+%\|[█▉▊▋▌▍▎▏ ]+\|.*", re.MULTILINE), "progress bar", ""),

    # Docker pull/build noise
    (re.compile(r"^\s*[a-f0-9]{12}: (Pull|Download|Extract).*", re.MULTILINE), "docker layer", ""),
    (re.compile(r"^\s*Status: Downloaded.*", re.MULTILINE), "docker status", ""),

    # Cargo/cmake noise
    (re.compile(r"^\s*Compiling\s+\S+", re.MULTILINE), "cargo compiling", ""),
    (re.compile(r"^\s*Checking\s+\S+", re.MULTILINE), "cargo checking", ""),

    # General: repeated identical lines (keep first occurrence)
    # (This needs a stateful pass — handled in snip_tool_result)
]


def snip_tool_result(content: str) -> str:
    """Trim noise from a single tool result string.

    Applies regex patterns and deduplicates repeated lines.
    """
    result = content

    # Apply regex replacements
    for pattern, _desc, replacement in _SNIP_PATTERNS:
        result = pattern.sub(replacement, result)

    # Collapse 3+ consecutive blank lines to 2
    result = re.sub(r"\n{4,}", "\n\n\n", result)

    # Remove leading/trailing whitespace while preserving intentional indentation
    result = result.strip()

    return result


def snip_messages(messages: list[dict]) -> list[dict]:
    """Apply L1 snip to all tool_result blocks in the message list.

    Each tool_result block's content is passed through snip_tool_result().
    Returns a new message list (no mutation of input).
    """
    result: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_content = []
        for block in content:
            if block.get("type") == "tool_result":
                raw = block.get("content", "")
                snipped = snip_tool_result(raw)
                new_content.append({**block, "content": snipped})
            else:
                new_content.append(block)

        result.append({**msg, "content": new_content})

    return result


# ── Snip-Compact ──────────────────────────────────────────────────────

_SNIP_TAG = "<snip>Content snipped to save context space</snip>"


def _is_tool_result_message(msg: dict[str, Any]) -> bool:
    """True if this is a user message carrying tool_result blocks."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(block.get("type") == "tool_result" for block in content)


def snip_compact_if_needed(
    messages: list[dict[str, Any]],
    preserve_last_n_turns: int = 2,
    snip_char_threshold: int = 2000,
) -> tuple[list[dict[str, Any]], int]:
    """Replace stale large tool results with a <snip> tag.

    Scans the message history for old user messages carrying tool_result
    blocks.  The most recent *N* such messages are preserved verbatim;
    anything older gets its oversized tool_result content replaced.

    Returns a new message list (deep copy) and the estimated number of
    tokens freed by snipping.
    """
    # 1. Deep copy — never mutate the caller's data
    result: list[dict[str, Any]] = copy.deepcopy(messages)

    # 2. Identify which user messages carry tool_result blocks
    tool_result_indices: list[int] = []
    for i, msg in enumerate(result):
        if _is_tool_result_message(msg):
            tool_result_indices.append(i)

    if not tool_result_indices:
        return result, 0

    # 3. Mark the last N such messages as "fresh" — must skip
    fresh_start = max(0, len(tool_result_indices) - preserve_last_n_turns)
    stale_indices: set[int] = set(tool_result_indices[:fresh_start])

    # 4. Scan stale messages and snip oversized tool_result blocks
    snip_tokens_freed = 0

    for i in stale_indices:
        msg = result[i]
        content_blocks: list[dict[str, Any]] = msg["content"]

        for block in content_blocks:
            if block.get("type") != "tool_result":
                continue
            original = block.get("content", "")
            if isinstance(original, str) and len(original) > snip_char_threshold:
                snip_tokens_freed += rough_estimate(original) - rough_estimate(_SNIP_TAG)
                block["content"] = _SNIP_TAG

    return result, snip_tokens_freed
