"""Formatting helpers for injecting persistent memory into conversations."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from .index import INDEX_FILENAME, load_memory_index
from .recall import MemoryRecall

MEMORY_META_KEY = "xxcode_memory_context"
MEMORY_INDEX_SOURCE = "memory_index"
MEMORY_RECALL_SOURCE = "memory_recall"


def build_memory_index_message(memory_dir: Path) -> dict | None:
    """Build the hidden user-context message for the current ``MEMORY.md``."""
    memory_index = load_memory_index(memory_dir)
    if not memory_index:
        memory_index = "(no indexed memories yet)"

    index_path = memory_dir / INDEX_FILENAME
    text = (
        "<system-reminder>\n"
        f"Contents of {index_path} "
        "(user's auto-memory, persists across conversations):\n\n"
        f"{memory_index}\n"
        "</system-reminder>"
    )
    return _meta_user_message(text, MEMORY_INDEX_SOURCE)


def build_recalled_memories_message(recalled: list[MemoryRecall]) -> dict | None:
    """Build a hidden user-context message for full recalled memory files."""
    parts: list[str] = []
    recall_ids: list[str] = []
    for memory in recalled:
        parts.append(f"{memory_header(memory.file_path)}\n\n{memory.content}")
        recall_ids.append(memory.recall_id or memory.filename)

    if not parts:
        return None

    combined = "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"
    message = _meta_user_message(combined, MEMORY_RECALL_SOURCE)
    message["metadata"]["filenames"] = [memory.filename for memory in recalled]
    message["metadata"]["recall_ids"] = recall_ids
    return message


def memory_header(path: Path, *, now: float | None = None) -> str:
    """Return a freshness-aware header for a recalled memory file."""
    if now is None:
        now = time.time()

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return f"Memory: {path}:"

    saved_date = date.fromtimestamp(mtime)
    current_date = date.fromtimestamp(now)
    age_days = max(0, (current_date - saved_date).days)
    if age_days == 0:
        return f"Memory (saved today): {path}:"
    if age_days == 1:
        return f"Memory (saved yesterday): {path}:"

    warning = (
        f"This memory is {age_days} days old. Memories are point-in-time "
        "observations, not live state - claims about code behavior or file:line "
        "citations may be outdated. Verify against current code before asserting "
        "as fact."
    )
    return f"{warning}\n\nMemory: {path}:"


def is_memory_context_message(message: dict) -> bool:
    """Return whether a message is an internal memory context injection."""
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and metadata.get(MEMORY_META_KEY) is True


def recalled_memory_filenames(messages: list[dict]) -> set[str]:
    """Return filenames currently present in recalled-memory injections."""
    filenames: set[str] = set()
    for message in messages:
        if not is_memory_context_message(message):
            continue
        metadata = message.get("metadata", {})
        if metadata.get("source") != MEMORY_RECALL_SOURCE:
            continue
        for filename in metadata.get("filenames", []):
            if isinstance(filename, str) and filename.endswith(".md"):
                filenames.add(filename)
    return filenames


def recalled_memory_ids(messages: list[dict], *, source: str = MEMORY_RECALL_SOURCE) -> set[str]:
    """Return stable recall identifiers from current memory injections."""
    recall_ids: set[str] = set()
    for message in messages:
        if not is_memory_context_message(message):
            continue
        metadata = message.get("metadata", {})
        if metadata.get("source") != source:
            continue
        values = metadata.get("recall_ids")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.endswith(".md"):
                    recall_ids.add(value)
            continue
        for filename in metadata.get("filenames", []):
            if isinstance(filename, str) and filename.endswith(".md"):
                recall_ids.add(filename)
    return recall_ids


def strip_memory_context_messages(
    messages: list[dict],
    *,
    source: str | None = None,
) -> list[dict]:
    """Remove prior runtime memory injections from a message list.

    When ``source`` is provided, only that kind of memory context is removed.
    This lets the agent refresh ``MEMORY.md`` every turn while keeping recalled
    full memories in conversation history.
    """
    kept: list[dict] = []
    for message in messages:
        if not is_memory_context_message(message):
            kept.append(message)
            continue
        metadata = message.get("metadata", {})
        if source is not None and metadata.get("source") != source:
            kept.append(message)
    return kept


def _meta_user_message(text: str, source: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "isMeta": True,
        "metadata": {
            MEMORY_META_KEY: True,
            "source": source,
        },
    }


def build_meta_user_message(text: str, source: str) -> dict:
    """Build a hidden user message for internal context injection."""
    return _meta_user_message(text, source)


__all__ = [
    "MEMORY_INDEX_SOURCE",
    "MEMORY_META_KEY",
    "MEMORY_RECALL_SOURCE",
    "build_memory_index_message",
    "build_meta_user_message",
    "build_recalled_memories_message",
    "is_memory_context_message",
    "recalled_memory_ids",
    "recalled_memory_filenames",
    "memory_header",
    "strip_memory_context_messages",
]
