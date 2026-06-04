"""Tests for memory models: MemoryType, MemoryEntry, YAML frontmatter parsing."""

import tempfile
from pathlib import Path

import pytest

from xxcode.memory.models import (
    MemoryEntry,
    MemoryType,
    parse_memory_file,
    serialize_memory_file,
    slugify_name,
)


class TestMemoryType:
    def test_values(self):
        assert MemoryType.USER == "user"
        assert MemoryType.FEEDBACK == "feedback"
        assert MemoryType.PROJECT == "project"
        assert MemoryType.REFERENCE == "reference"

    def test_is_str_enum(self):
        assert isinstance(MemoryType.USER, str)


class TestMemoryEntry:
    def test_defaults(self):
        entry = MemoryEntry(name="test", description="desc")
        assert entry.name == "test"
        assert entry.description == "desc"
        assert entry.content == ""
        assert entry.metadata == {"type": "user"}
        assert entry.file_path is None

    def test_filename(self):
        entry = MemoryEntry(name="my-memory", description="")
        assert entry.filename == "my-memory.md"

    def test_filename_prefers_existing_file_path(self):
        entry = MemoryEntry(
            name="renamed-memory",
            description="",
            file_path=Path("/tmp/original-file.md"),
        )
        assert entry.filename == "original-file.md"
        assert entry.slug_filename == "renamed-memory.md"

    def test_memory_type_property(self):
        entry = MemoryEntry(name="t", description="", metadata={"type": "feedback"})
        assert entry.memory_type == MemoryType.FEEDBACK

    def test_memory_type_default(self):
        entry = MemoryEntry(name="t", description="", metadata={})
        assert entry.memory_type == MemoryType.USER

    def test_memory_type_unknown_falls_back_to_user(self):
        entry = MemoryEntry(name="t", description="", metadata={"type": "preference"})
        assert entry.memory_type == MemoryType.USER

    def test_filename_slugifies_name(self):
        entry = MemoryEntry(name="../User Preference!", description="")
        assert entry.filename == "user-preference.md"


class TestParseMemoryFile:
    def test_valid_frontmatter(self):
        content = """---
name: test-entry
description: A test description
metadata:
  type: feedback
---
Body content here.
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            entry = parse_memory_file(path)
            assert entry is not None
            assert entry.name == "test-entry"
            assert entry.description == "A test description"
            assert entry.content == "Body content here."
            assert entry.metadata == {"type": "feedback"}
        finally:
            path.unlink()

    def test_no_frontmatter(self):
        content = "Just body, no frontmatter"
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            entry = parse_memory_file(path)
            assert entry is not None
            # Falls back to filename stem as name
            assert entry.name == path.stem
            assert entry.description == ""
            assert entry.content == "Just body, no frontmatter"
        finally:
            path.unlink()

    def test_empty_description(self):
        content = """---
name: minimal
---
Body
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            entry = parse_memory_file(path)
            assert entry is not None
            assert entry.description == ""
        finally:
            path.unlink()

    def test_missing_file(self):
        entry = parse_memory_file(Path("/nonexistent/path.md"))
        assert entry is None

    def test_roundtrip(self):
        entry = MemoryEntry(
            name="roundtrip-test",
            description="Test roundtrip",
            content="Content goes here.",
            metadata={"type": "project", "extra": "value"},
        )
        serialized = serialize_memory_file(entry)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(serialized)
            path = Path(f.name)

        try:
            parsed = parse_memory_file(path)
            assert parsed is not None
            assert parsed.name == entry.name
            assert parsed.description == entry.description
            assert parsed.content == entry.content
            assert parsed.metadata["type"] == "project"
            assert parsed.metadata["extra"] == "value"
        finally:
            path.unlink()

    def test_string_metadata(self):
        """When metadata is a string (e.g. 'user'), wrap it in a dict."""
        content = """---
name: simple
description: Simple
metadata: user
---
Body
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = Path(f.name)

        try:
            entry = parse_memory_file(path)
            assert entry is not None
            assert entry.metadata == {"type": "user"}
        finally:
            path.unlink()


class TestSerializeMemoryFile:
    def test_basic_serialization(self):
        entry = MemoryEntry(
            name="my-mem",
            description="desc",
            content="Body text",
        )
        output = serialize_memory_file(entry)
        assert "name: my-mem" in output
        assert "description: desc" in output
        assert "type: user" in output
        assert "Body text" in output
        assert output.startswith("---")
        assert output.endswith("\n")


class TestSlugifyName:
    def test_lowercase(self):
        assert slugify_name("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify_name("Hello! @World #2024") == "hello-world-2024"

    def test_multiple_spaces(self):
        assert slugify_name("a   b") == "a-b"

    def test_multiple_hyphens(self):
        assert slugify_name("a---b") == "a-b"

    def test_leading_trailing(self):
        assert slugify_name("-hello-") == "hello"

    def test_empty(self):
        assert slugify_name("") == "untitled"

    def test_only_special(self):
        assert slugify_name("@#$%") == "untitled"

    def test_chinese_chars(self):
        result = slugify_name("测试记忆")
        assert result == "测试记忆"
