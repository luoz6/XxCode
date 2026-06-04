"""Tests for MemoryStore CRUD operations."""

import tempfile
from pathlib import Path

import pytest

from xxcode.memory.models import MemoryEntry
from xxcode.memory.store import MemoryStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield MemoryStore(Path(tmp))


class TestMemoryStore:
    def test_save_and_get(self, store):
        entry = MemoryEntry(name="test", description="Test", content="Hello")
        store.save_entry(entry)

        loaded = store.get_entry("test")
        assert loaded is not None
        assert loaded.name == "test"
        assert loaded.description == "Test"
        assert loaded.content == "Hello"

    def test_get_nonexistent(self, store):
        assert store.get_entry("nonexistent") is None

    def test_list_entries(self, store):
        store.save_entry(MemoryEntry(name="a", description="A"))
        store.save_entry(MemoryEntry(name="b", description="B"))

        entries = store.list_entries()
        assert len(entries) == 2
        names = {e.name for e in entries}
        assert names == {"a", "b"}

    def test_list_excludes_memoy_md(self, store):
        """MEMORY.md should not appear in entry listings."""
        store.save_entry(MemoryEntry(name="real", description="Real"))
        (store.directory / "MEMORY.md").write_text("index content")

        entries = store.list_entries()
        names = {e.name for e in entries}
        assert "MEMORY" not in names
        assert "real" in names

    def test_entry_count(self, store):
        assert store.entry_count() == 0
        store.save_entry(MemoryEntry(name="one", description="One"))
        assert store.entry_count() == 1
        store.save_entry(MemoryEntry(name="two", description="Two"))
        assert store.entry_count() == 2

    def test_entry_count_excludes_memoy_md(self, store):
        store.save_entry(MemoryEntry(name="real", description="Real"))
        (store.directory / "MEMORY.md").write_text("index")
        assert store.entry_count() == 1

    def test_delete(self, store):
        store.save_entry(MemoryEntry(name="to-delete", description="X"))
        assert store.get_entry("to-delete") is not None

        result = store.delete_entry("to-delete")
        assert result is True
        assert store.get_entry("to-delete") is None

    def test_delete_nonexistent(self, store):
        result = store.delete_entry("nonexistent")
        assert result is False

    def test_delete_preserves_file_when_index_update_fails(self, store, monkeypatch):
        store.save_entry(MemoryEntry(name="keep-on-failure", description="X"))
        target = store.directory / "keep-on-failure.md"

        def _fail_remove_index(_filename):
            raise OSError("simulated index failure")

        monkeypatch.setattr(store, "_remove_index_entry", _fail_remove_index)

        with pytest.raises(OSError):
            store.delete_entry("keep-on-failure")

        assert target.exists()
        assert store.get_entry("keep-on-failure") is not None
        index_content = (store.directory / "MEMORY.md").read_text(encoding="utf-8")
        assert "keep-on-failure.md" in index_content

    def test_delete_rebuilds_index_when_file_delete_fails(self, store, monkeypatch):
        store.save_entry(MemoryEntry(name="delete-failure", description="X"))
        target = store.directory / "delete-failure.md"
        real_unlink = Path.unlink

        def _fail_target_unlink(path, *args, **kwargs):
            if path == target:
                raise OSError("simulated unlink failure")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _fail_target_unlink)

        with pytest.raises(OSError):
            store.delete_entry("delete-failure")

        assert target.exists()
        index_content = (store.directory / "MEMORY.md").read_text(encoding="utf-8")
        assert "delete-failure.md" in index_content

    def test_save_overwrites(self, store):
        v1 = MemoryEntry(name="same", description="First", content="v1")
        store.save_entry(v1)

        v2 = MemoryEntry(name="same", description="Second", content="v2")
        store.save_entry(v2)

        loaded = store.get_entry("same")
        assert loaded.content == "v2"
        assert loaded.description == "Second"

    def test_file_exists_on_disk(self, store):
        entry = MemoryEntry(name="disk-test", description="Desc", content="Body")
        file_path = store.save_entry(entry)
        assert file_path.exists()
        assert file_path.suffix == ".md"

    def test_save_slugifies_filename(self, store):
        file_path = store.save_entry(MemoryEntry(
            name="../User Preference!",
            description="Desc",
            content="Body",
        ))

        assert file_path.name == "user-preference.md"
        assert file_path.parent == store.directory
        assert store.get_entry("../User Preference!") is not None

    def test_save_does_not_escape_directory_for_path_like_name(self, store):
        file_path = store.save_entry(MemoryEntry(
            name="../../outside/secret",
            description="Desc",
            content="Body",
        ))

        assert file_path == store.directory / "outsidesecret.md"
        assert file_path.exists()
        assert not (store.directory.parent / "outside").exists()

    def test_save_entry_cleans_up_temp_file(self, store):
        store.save_entry(MemoryEntry(name="atomic", description="Desc", content="Body"))

        leftovers = list(store.directory.glob("*.tmp"))
        assert leftovers == []

    def test_failed_atomic_replace_preserves_existing_file(self, store, monkeypatch):
        original = MemoryEntry(name="atomic", description="Original", content="v1")
        store.save_entry(original)

        def _fail_replace(_src, _dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr("xxcode.memory.store.os.replace", _fail_replace)

        with pytest.raises(OSError):
            store.save_entry(MemoryEntry(
                name="atomic",
                description="Updated",
                content="v2",
            ))

        loaded = store.get_entry("atomic")
        assert loaded is not None
        assert loaded.description == "Original"
        assert loaded.content == "v1"
        assert list(store.directory.glob("*.tmp")) == []

    def test_save_rename_moves_file_and_removes_old_name(self, store):
        entry = MemoryEntry(name="old-name", description="Original", content="v1")
        original_path = store.save_entry(entry)

        entry.name = "new-name"
        updated_path = store.save_entry(entry)

        assert updated_path.name == "new-name.md"
        assert updated_path.exists()
        assert not original_path.exists()

        loaded = store.get_entry("new-name")
        assert loaded is not None
        assert loaded.filename == "new-name.md"
