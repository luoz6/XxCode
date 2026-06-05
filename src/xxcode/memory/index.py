"""MEMORY.md index generation and loading.

The runtime memory entrypoint is ``MEMORY.md``. Individual memory files keep
the full durable content, while the index is a compact map that is loaded into
the system prompt and used as the recall candidate list.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .models import MemoryType
from .store import MemoryStore

INDEX_FILENAME = "MEMORY.md"

logger = logging.getLogger(__name__)

_LINE_BUDGET = 150
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000

_TYPE_ORDER = {
    MemoryType.USER: 0,
    MemoryType.PROJECT: 1,
    MemoryType.FEEDBACK: 2,
    MemoryType.REFERENCE: 3,
}


@dataclass
class EntrypointTruncation:
    """Result of truncating index content to fit context budget."""

    content: str
    line_count: int
    byte_count: int
    was_line_truncated: bool
    was_byte_truncated: bool


@dataclass(frozen=True)
class MemoryIndexEntry:
    """A parsed entry from MEMORY.md."""

    title: str
    filename: str
    description: str = ""


def _title_case_slug(slug: str) -> str:
    """Convert a kebab slug to Title Case for display."""
    return " ".join(word.capitalize() for word in slug.replace("_", "-").split("-"))


def _make_index_line(entry) -> str:
    """Format a single index line: ``- [Title](file.md) - hook``."""
    title = _title_case_slug(entry.name)
    link = entry.filename
    hook = entry.description

    prefix = f"- [{title}]({link}) - "
    available = max(_LINE_BUDGET - len(prefix), 10)
    if len(hook) > available:
        hook = hook[: max(available - 3, 1)] + "..."

    return f"{prefix}{hook}"


def truncate_entrypoint_content(raw: str) -> EntrypointTruncation:
    """Apply line and byte limits to MEMORY.md content."""
    trimmed = raw.rstrip()
    content_lines = trimmed.split("\n")
    line_count = len(content_lines)
    byte_count = len(trimmed.encode("utf-8"))

    was_line_truncated = line_count > MAX_ENTRYPOINT_LINES
    was_byte_truncated = False

    truncated = trimmed
    reasons: list[str] = []

    if was_line_truncated:
        truncated = "\n".join(content_lines[:MAX_ENTRYPOINT_LINES])
        reasons.append("too many entries")

    encoded = truncated.encode("utf-8")
    if len(encoded) > MAX_ENTRYPOINT_BYTES:
        chunk = encoded[:MAX_ENTRYPOINT_BYTES]
        last_nl = chunk.rfind(b"\n")
        if last_nl > 0:
            truncated = chunk[:last_nl].decode("utf-8")
        else:
            for i in range(len(chunk), 0, -1):
                try:
                    truncated = chunk[:i].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    continue
        was_byte_truncated = True
        reasons.append("too large")

    if was_line_truncated or was_byte_truncated:
        reason = " and ".join(reasons)
        logger.warning(
            "MEMORY.md truncated: %s (original=%d lines, %d bytes, limit=%d lines, %d bytes)",
            reason,
            line_count,
            byte_count,
            MAX_ENTRYPOINT_LINES,
            MAX_ENTRYPOINT_BYTES,
        )
        truncated += (
            f"\n\n> WARNING: MEMORY.md is {reason}. "
            "Only part of it was loaded."
        )

    return EntrypointTruncation(
        content=truncated,
        line_count=line_count,
        byte_count=byte_count,
        was_line_truncated=was_line_truncated,
        was_byte_truncated=was_byte_truncated,
    )


def generate_memory_index(memory_dir: Path) -> str:
    """Build MEMORY.md from all stored memory entries."""
    store = MemoryStore(memory_dir)
    entries = store.list_entries()
    if not entries:
        return ""

    entries.sort(key=lambda e: (_TYPE_ORDER.get(e.memory_type, 99), e.filename))
    raw = "\n".join(_make_index_line(e) for e in entries) + "\n"
    return truncate_entrypoint_content(raw).content


def update_index_entry(memory_dir: Path, entry) -> str:
    """Incrementally add or update a single entry in the index.

    Reads the existing MEMORY.md, replaces the line for this entry's file
    (or appends it). Does NOT re-sort — ordering may drift between full
    rebuilds. Use write_memory_index() for a sorted rebuild.
    """
    index_path = memory_dir / INDEX_FILENAME
    new_line = _make_index_line(entry)
    target_filename = entry.filename

    if not index_path.exists():
        return generate_memory_index(memory_dir)

    try:
        existing = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return generate_memory_index(memory_dir)

    lines = existing.splitlines()

    # If the index contains a truncation warning, fall back to full rebuild
    if any(line.startswith("> WARNING: MEMORY.md") for line in lines):
        return generate_memory_index(memory_dir)

    updated = False
    new_lines: list[str] = []
    for line in lines:
        parsed = parse_memory_index(line)
        parsed_filename = parsed[0].filename if parsed else None
        if parsed_filename == target_filename:
            new_lines.append(new_line)
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(new_line)

    raw = "\n".join(new_lines) + "\n"
    return truncate_entrypoint_content(raw).content


def remove_index_entry(memory_dir: Path, filename: str) -> str:
    """Incrementally remove a single entry from the index.

    Reads the existing MEMORY.md and removes the line referencing the
    given filename. Falls back to full rebuild if the index is missing.
    """
    index_path = memory_dir / INDEX_FILENAME

    if not index_path.exists():
        return generate_memory_index(memory_dir)

    try:
        existing = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return generate_memory_index(memory_dir)

    lines = existing.splitlines()
    new_lines: list[str] = []
    for line in lines:
        parsed = parse_memory_index(line)
        parsed_filename = parsed[0].filename if parsed else None
        if parsed_filename == filename:
            continue
        new_lines.append(line)

    if not new_lines:
        return ""

    raw = "\n".join(new_lines) + "\n"
    return truncate_entrypoint_content(raw).content


def write_memory_index(memory_dir: Path) -> Path:
    """Generate MEMORY.md and write it atomically to the memory directory."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    content = generate_memory_index(memory_dir)
    index_path = memory_dir / INDEX_FILENAME
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{INDEX_FILENAME}.",
        suffix=".tmp",
        dir=memory_dir,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_path, index_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return index_path


def load_memory_index(memory_dir: Path) -> str:
    """Read MEMORY.md, applying entrypoint truncation on load."""
    index_path = memory_dir / INDEX_FILENAME
    if not index_path.exists():
        return ""
    try:
        raw = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return truncate_entrypoint_content(raw).content


def parse_memory_index(content: str) -> list[MemoryIndexEntry]:
    """Parse markdown links from MEMORY.md.

    Supported lines look like ``- [Title](file.md) - description``. The parser
    is intentionally tolerant so older indexes with dashes or colons still work.
    """
    entries: list[MemoryIndexEntry] = []
    seen: set[str] = set()
    line_re = re.compile(r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)\s*(?P<tail>.*)$")
    for line in content.splitlines():
        match = line_re.match(line)
        if not match:
            continue
        filename = Path(match.group("file")).name
        if filename == INDEX_FILENAME or filename in seen:
            continue
        tail = match.group("tail").strip()
        description = re.sub(r"^(?:[-:]\s*)+", "", tail).strip()
        entries.append(
            MemoryIndexEntry(
                title=match.group("title").strip(),
                filename=filename,
                description=description,
            )
        )
        seen.add(filename)
    return entries
