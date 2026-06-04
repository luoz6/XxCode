"""Semantic memory recall engine backed by MEMORY.md.

On each user query, MEMORY.md is used as the entrypoint candidate list. A
lightweight side-query selects up to five indexed memories, then the selected
full memory files are loaded and injected into the conversation.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .index import load_memory_index, parse_memory_index
from .models import MemoryType, parse_memory_file
from .cleanup import touch_memory_access

logger = logging.getLogger(__name__)

MAX_RECALLED_MEMORIES = 5

_SELECT_MEMORIES_SYSTEM_PROMPT = """\
You are selecting memories that will be useful to an AI coding agent as it
processes a user's query. Return a JSON array of filenames for memories that
are clearly useful (up to 5).

- Be selective and discerning; only pick memories with high relevance.
- Prefer recent memories when the index or filename suggests recency.
- If recently-used tools are provided, do NOT select usage reference docs for
  those tools. DO still select warnings, gotchas, or known issues about them.
- Return ONLY the JSON array, nothing else.

Format: ["filename.md", ...]"""


@dataclass
class MemoryRecall:
    """A single recalled memory with its full content."""

    filename: str
    file_path: Path
    content: str
    memory_type: MemoryType
    recall_id: str | None = None


ClientFactory = Callable[[], Awaitable[object]]


def _manifest_from_memory_index(index_content: str) -> str:
    """Convert MEMORY.md links into the selector manifest format."""
    entries = parse_memory_index(index_content)
    if not entries:
        return "(no memories available)"
    return "\n".join(
        f"- [indexed] {entry.filename}: {entry.description}"
        for entry in entries
    )


def _parse_selection_response(text: str) -> list[str]:
    """Extract a list of filenames from a model's JSON response."""
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    array_match = re.search(r"\[.*?\]", text, re.DOTALL)
    if not array_match:
        return []

    try:
        result = json.loads(array_match.group(0))
    except json.JSONDecodeError:
        return []

    if isinstance(result, list) and all(isinstance(x, str) for x in result):
        return result
    return []


def _valid_names_from_manifest(manifest: str) -> set[str]:
    """Extract valid filenames from supported selector manifest lines."""
    valid_names: set[str] = set()
    for line in manifest.splitlines():
        if not line.startswith("- ["):
            continue
        match = re.match(r"- \[[^]]*\]\s+(?P<filename>[^\s(:]+\.md)", line)
        if match:
            valid_names.add(match.group("filename").strip())
    return valid_names


async def select_relevant_memories(
    query: str,
    manifest: str,
    *,
    client_factory: Callable[[], Awaitable[object]],
    recent_tools: list[str] | None = None,
    already_surfaced: set[str] | None = None,
) -> list[str]:
    """Ask a lightweight model to pick up to ``MAX_RECALLED_MEMORIES`` filenames."""
    if already_surfaced is None:
        already_surfaced = set()

    user_parts = [f"Query: {query}", "", "Available memories:", manifest]

    if recent_tools:
        user_parts.extend(["", f"Recently used tools: {', '.join(recent_tools)}"])

    surfaced = {f for f in already_surfaced if f.endswith(".md")}
    if surfaced:
        user_parts.extend([
            "",
            "Already shown (do NOT select these): " + ", ".join(sorted(surfaced)),
        ])

    try:
        client = await client_factory()
        response_text = await client.complete(
            system_prompt=_SELECT_MEMORIES_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(user_parts)}],
            max_tokens=256,
        )
    except Exception:
        logger.debug("Memory recall side-query failed", exc_info=True)
        return []

    valid_names = _valid_names_from_manifest(manifest)
    selected = _parse_selection_response(response_text)
    return [f for f in selected if f in valid_names][:MAX_RECALLED_MEMORIES]


async def recall_memories_for_query(
    query: str,
    memory_dir: Path,
    *,
    client_factory: Callable[[], Awaitable[object]],
    recent_tools: list[str] | None = None,
    already_surfaced: set[str] | None = None,
) -> list[MemoryRecall]:
    """Full memory recall pipeline: MEMORY.md -> select -> load."""
    if already_surfaced is None:
        already_surfaced = set()

    index_content = load_memory_index(memory_dir)
    indexed_entries = parse_memory_index(index_content)
    if not indexed_entries:
        return []

    indexed_names = {entry.filename for entry in indexed_entries}
    manifest = _manifest_from_memory_index(index_content)

    selected = await select_relevant_memories(
        query=query,
        manifest=manifest,
        client_factory=client_factory,
        recent_tools=recent_tools,
        already_surfaced=already_surfaced,
    )
    if not selected:
        return []

    surfaced = {name for name in already_surfaced if name.endswith(".md")}

    results: list[MemoryRecall] = []
    for filename in selected:
        if filename in surfaced:
            continue
        if filename not in indexed_names:
            continue
        file_path = memory_dir / filename
        if not file_path.exists():
            continue
        try:
            full_text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        parsed = parse_memory_file(file_path)
        results.append(
            MemoryRecall(
                filename=filename,
                file_path=file_path,
                content=full_text,
                memory_type=parsed.memory_type if parsed else MemoryType.USER,
                recall_id=filename,
            )
        )
        touch_memory_access(file_path)

    return results
