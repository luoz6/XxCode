"""Memory data types: MemoryType enum, MemoryEntry dataclass, YAML frontmatter parsing."""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml


class MemoryType(StrEnum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


@dataclass
class MemoryEntry:
    name: str
    description: str
    content: str = ""
    metadata: dict = field(default_factory=lambda: {"type": "user"})
    file_path: Path | None = field(default=None, repr=False)

    @property
    def memory_type(self) -> MemoryType:
        t = self.metadata.get("type", "user")
        if isinstance(t, MemoryType):
            return t
        try:
            return MemoryType(t)
        except ValueError:
            return MemoryType.USER

    @property
    def filename(self) -> str:
        if self.file_path is not None:
            return self.file_path.name
        return f"{slugify_name(self.name)}.md"

    @property
    def slug_filename(self) -> str:
        return f"{slugify_name(self.name)}.md"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata_dict, body_text)."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    try:
        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        return {}, text

    body = parts[2].strip()
    return meta, body


def parse_memory_file(path: Path) -> MemoryEntry | None:
    """Read a .md file and parse into a MemoryEntry. Returns None on failure."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    meta, body = _parse_frontmatter(text)
    name = meta.get("name", path.stem)
    description = meta.get("description", "")
    metadata = meta.get("metadata", {})

    if isinstance(metadata, str):
        metadata = {"type": metadata}

    return MemoryEntry(
        name=name,
        description=description,
        content=body,
        metadata=metadata,
        file_path=path,
    )


def serialize_memory_file(entry: MemoryEntry) -> str:
    """Serialize a MemoryEntry to a full .md file string with YAML frontmatter."""
    frontmatter = {
        "name": entry.name,
        "description": entry.description,
        "metadata": entry.metadata,
    }
    yaml_str = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    body = entry.content.strip()
    return f"---\n{yaml_str}\n---\n\n{body}\n"


def slugify_name(text: str) -> str:
    """Convert arbitrary text to a kebab-slug suitable for a filename."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[_\s]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-") or "untitled"
