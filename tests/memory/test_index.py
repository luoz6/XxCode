"""Tests for MEMORY.md index generation and loading."""

import logging
import tempfile
from pathlib import Path

import pytest

from xxcode.memory.index import (
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    EntrypointTruncation,
    generate_memory_index,
    load_memory_index,
    parse_memory_index,
    truncate_entrypoint_content,
    write_memory_index,
)
from xxcode.memory.models import MemoryEntry
from xxcode.memory.store import MemoryStore


@pytest.fixture
def mem_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


class TestGenerateMemoryIndex:
    def test_empty_directory(self, mem_dir):
        assert generate_memory_index(mem_dir) == ""

    def test_single_entry(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="user-role", description="User is a data scientist"))

        result = generate_memory_index(mem_dir)
        assert "- [User Role](user-role.md)" in result
        assert "data scientist" in result

    def test_entries_sorted_by_type(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="ref-item",
            description="Reference",
            metadata={"type": "reference"},
        ))
        store.save_entry(MemoryEntry(
            name="user-item",
            description="User",
            metadata={"type": "user"},
        ))

        lines = generate_memory_index(mem_dir).strip().split("\n")
        assert "user-item" in lines[0]
        assert "ref-item" in lines[1]

    def test_description_truncation(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="test", description="X" * 200))

        assert "..." in generate_memory_index(mem_dir)

    def test_max_entries_truncation(self, mem_dir):
        store = MemoryStore(mem_dir)
        for i in range(210):
            store.save_entry(MemoryEntry(
                name=f"entry-{i}",
                description=f"Description {i}",
            ))

        result = generate_memory_index(mem_dir)
        lines = [line for line in result.split("\n") if line.strip()]
        assert len(lines) <= 204
        assert "WARNING: MEMORY.md is too many entries" in result
        assert "Only part of it was loaded" in result

    def test_memory_md_not_included(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="real-one", description="Should appear"))
        (mem_dir / "MEMORY.md").write_text("fake index content")

        result = generate_memory_index(mem_dir)
        assert "real-one" in result
        assert "fake index content" not in result

    def test_uses_actual_filename_when_frontmatter_name_changes(self, mem_dir):
        path = mem_dir / "original-name.md"
        path.write_text(
            "---\n"
            "name: changed display name\n"
            "description: Still reachable\n"
            "metadata:\n"
            "  type: user\n"
            "---\n\n"
            "Body\n",
            encoding="utf-8",
        )

        result = generate_memory_index(mem_dir)
        assert "(original-name.md)" in result
        assert "(changed-display-name.md)" not in result


class TestTruncateEntrypointContent:
    def test_no_truncation_needed(self):
        raw = "- [One](one.md) - first\n- [Two](two.md) - second\n"
        result = truncate_entrypoint_content(raw)
        assert not result.was_line_truncated
        assert not result.was_byte_truncated
        assert result.content.strip() == raw.strip()

    def test_line_truncation(self):
        lines = [
            f"- [Entry {i}](entry-{i}.md) - desc {i}"
            for i in range(MAX_ENTRYPOINT_LINES + 50)
        ]
        result = truncate_entrypoint_content("\n".join(lines) + "\n")
        assert result.was_line_truncated
        assert result.line_count == MAX_ENTRYPOINT_LINES + 50
        assert "WARNING" in result.content

    def test_byte_truncation(self):
        long_line = "- [X](x.md) - " + "A" * (MAX_ENTRYPOINT_BYTES // 2)
        result = truncate_entrypoint_content("\n".join([long_line] * 3) + "\n")
        assert result.was_byte_truncated
        assert "WARNING: MEMORY.md is too large" in result.content

    def test_logs_warning_when_truncated(self, caplog):
        long_line = "- [X](x.md) - " + "A" * (MAX_ENTRYPOINT_BYTES // 2)

        with caplog.at_level(logging.WARNING):
            result = truncate_entrypoint_content("\n".join([long_line] * 3) + "\n")

        assert result.was_byte_truncated
        assert "MEMORY.md truncated" in caplog.text

    def test_combined_truncation(self):
        long_line = "- [X](x.md) - " + "B" * 500
        result = truncate_entrypoint_content(
            "\n".join([long_line] * (MAX_ENTRYPOINT_LINES + 20)) + "\n"
        )
        assert result.was_line_truncated
        assert result.was_byte_truncated
        assert "too many entries and too large" in result.content

    def test_metadata_fields(self):
        raw = "- [One](one.md) - first\n- [Two](two.md) - second\n"
        result = truncate_entrypoint_content(raw)

        assert isinstance(result, EntrypointTruncation)
        assert result.line_count == 2
        assert result.byte_count > 0
        assert isinstance(result.was_line_truncated, bool)
        assert isinstance(result.was_byte_truncated, bool)

    def test_empty_string(self):
        result = truncate_entrypoint_content("")
        assert result.content == ""
        assert result.line_count == 1
        assert result.byte_count == 0


class TestWriteMemoryIndex:
    def test_creates_file(self, mem_dir):
        path = write_memory_index(mem_dir)
        assert path.exists()
        assert path.name == "MEMORY.md"

    def test_overwrites(self, mem_dir):
        (mem_dir / "MEMORY.md").write_text("old content")
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="entry", description="New"))

        write_memory_index(mem_dir)
        content = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "old content" not in content
        assert "New" in content


class TestLoadMemoryIndex:
    def test_load_existing(self, mem_dir):
        (mem_dir / "MEMORY.md").write_text("index content here")
        assert load_memory_index(mem_dir) == "index content here"

    def test_load_missing(self, mem_dir):
        assert load_memory_index(mem_dir) == ""


class TestParseMemoryIndex:
    def test_parses_markdown_links(self):
        entries = parse_memory_index(
            "- [User Role](user-role.md) - User is a data scientist\n"
            "- [Project Deadline](project-deadline.md) - Q3 deadline\n"
        )

        assert [entry.filename for entry in entries] == [
            "user-role.md",
            "project-deadline.md",
        ]
        assert entries[0].description == "User is a data scientist"

    def test_ignores_memory_md_and_duplicates(self):
        entries = parse_memory_index(
            "- [Index](MEMORY.md) - ignored\n"
            "- [A](a.md) - first\n"
            "- [A Again](a.md) - duplicate\n"
        )

        assert [entry.filename for entry in entries] == ["a.md"]
