"""Tests for incremental index update and removal."""

import tempfile
from pathlib import Path

import pytest

from xxcode.memory.index import (
    INDEX_FILENAME,
    generate_memory_index,
    remove_index_entry,
    update_index_entry,
)
from xxcode.memory.models import MemoryEntry
from xxcode.memory.store import MemoryStore


@pytest.fixture
def mem_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


class TestUpdateIndexEntry:
    def test_adds_new_entry_to_existing_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="first", description="First entry", content="Body"))

        # Now add a second entry incrementally
        new_entry = MemoryEntry(name="second", description="Second entry", content="Body 2")
        (mem_dir / new_entry.filename).write_text("---\nname: second\n---\nBody 2\n")

        content = update_index_entry(mem_dir, new_entry)
        assert "first.md" in content
        assert "second.md" in content

    def test_updates_existing_entry_description(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="target", description="Old description", content="Body"))

        index_content = (mem_dir / INDEX_FILENAME).read_text(encoding="utf-8")
        assert "Old description" in index_content

        updated_entry = MemoryEntry(name="target", description="New description", content="Body v2")
        content = update_index_entry(mem_dir, updated_entry)
        assert "New description" in content
        assert "Old description" not in content

    def test_creates_index_when_missing(self, mem_dir):
        mem_dir.mkdir(parents=True, exist_ok=True)
        entry = MemoryEntry(name="solo", description="Solo entry", content="Body")
        (mem_dir / entry.filename).write_text("---\nname: solo\n---\nBody\n")

        content = update_index_entry(mem_dir, entry)
        assert "solo.md" in content

    def test_does_not_duplicate_entry(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="dup", description="Version 1", content="Body"))

        updated = MemoryEntry(name="dup", description="Version 2", content="Body v2")
        content = update_index_entry(mem_dir, updated)

        # Should only appear once
        assert content.count("dup.md") == 1
        assert "Version 2" in content

    def test_matches_exact_filename_when_updating(self, mem_dir):
        (mem_dir / INDEX_FILENAME).write_text(
            "- [A](a.md) - first\n"
            "- [A Backup](a-backup.md) - second\n",
            encoding="utf-8",
        )

        updated = MemoryEntry(name="a", description="updated")
        content = update_index_entry(mem_dir, updated)

        assert "- [A](a.md) - updated" in content
        assert "- [A Backup](a-backup.md) - second" in content


class TestRemoveIndexEntry:
    def test_removes_entry_from_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="keep", description="Keep this", content="Body"))
        store.save_entry(MemoryEntry(name="remove", description="Remove this", content="Body"))

        content = remove_index_entry(mem_dir, "remove.md")
        assert "remove.md" not in content
        assert "keep.md" in content

    def test_removes_nonexistent_entry_is_noop(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="only", description="Only entry", content="Body"))

        content = remove_index_entry(mem_dir, "ghost.md")
        assert "only.md" in content

    def test_removes_last_entry_returns_empty(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="last", description="Last one", content="Body"))

        content = remove_index_entry(mem_dir, "last.md")
        assert content == ""

    def test_creates_index_when_missing(self, mem_dir):
        mem_dir.mkdir(parents=True, exist_ok=True)
        content = remove_index_entry(mem_dir, "anything.md")
        assert content == ""

    def test_removes_exact_filename_only(self, mem_dir):
        (mem_dir / INDEX_FILENAME).write_text(
            "- [A](a.md) - first\n"
            "- [A Backup](a-backup.md) - second\n",
            encoding="utf-8",
        )

        content = remove_index_entry(mem_dir, "a.md")
        assert "a.md" not in content
        assert "a-backup.md" in content


class TestStoreIncrementalIndex:
    """Verify that MemoryStore uses incremental updates correctly."""

    def test_save_entry_updates_index_incrementally(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="a", description="Entry A", content="Body A"))
        store.save_entry(MemoryEntry(name="b", description="Entry B", content="Body B"))

        index_content = (mem_dir / INDEX_FILENAME).read_text(encoding="utf-8")
        assert "a.md" in index_content
        assert "b.md" in index_content

    def test_delete_entry_removes_from_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="keep", description="Keep", content="Body"))
        store.save_entry(MemoryEntry(name="delete-me", description="Delete", content="Body"))

        store.delete_entry("delete-me")

        index_content = (mem_dir / INDEX_FILENAME).read_text(encoding="utf-8")
        assert "delete-me.md" not in index_content
        assert "keep.md" in index_content

    def test_save_overwrite_updates_description_in_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="evolve", description="V1", content="Body"))
        store.save_entry(MemoryEntry(name="evolve", description="V2", content="Body updated"))

        index_content = (mem_dir / INDEX_FILENAME).read_text(encoding="utf-8")
        assert "V2" in index_content
        assert "V1" not in index_content
        assert index_content.count("evolve.md") == 1

    def test_refresh_index_still_works_for_full_rebuild(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="x", description="X", content="Body"))
        store.save_entry(MemoryEntry(name="y", description="Y", content="Body"))

        # Corrupt the index
        (mem_dir / INDEX_FILENAME).write_text("corrupted content")

        # Full rebuild should fix it
        store.refresh_index()
        index_content = (mem_dir / INDEX_FILENAME).read_text(encoding="utf-8")
        assert "x.md" in index_content
        assert "y.md" in index_content
