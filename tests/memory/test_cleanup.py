"""Tests for memory cleanup: TTL expiration and access-frequency eviction."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from xxcode.memory.cleanup import (
    DEFAULT_MAX_MEMORIES,
    DEFAULT_TTL_DAYS,
    CleanupStats,
    cleanup_expired_memories,
    evict_least_accessed,
    run_cleanup,
    touch_memory_access,
)
from xxcode.memory.models import MemoryEntry, MemoryType
from xxcode.memory.store import MemoryStore


@pytest.fixture
def mem_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


class TestTouchMemoryAccess:
    def test_updates_atime(self, mem_dir):
        store = MemoryStore(mem_dir)
        path = store.save_entry(MemoryEntry(
            name="test", description="Test", content="Body",
        ))
        old_stat = path.stat()
        time.sleep(0.05)
        touch_memory_access(path)
        new_stat = path.stat()
        assert new_stat.st_atime >= old_stat.st_atime
        # mtime should remain unchanged
        assert new_stat.st_mtime == old_stat.st_mtime
        sidecar = mem_dir / ".access-times" / "test.md.atime"
        assert sidecar.exists()

    def test_nonexistent_file_no_error(self, mem_dir):
        touch_memory_access(mem_dir / "nonexistent.md")


class TestCleanupExpiredMemories:
    def test_removes_expired_memories(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="old-project",
            description="Old project info",
            content="Stale",
            metadata={"type": "project"},
        ))
        # Simulate file being very old (PROJECT TTL = 60 days)
        old_time = time.time() - (61 * 86400)
        path = mem_dir / "old-project.md"
        touch_memory_access(path)
        (mem_dir / ".access-times" / "old-project.md.atime").write_text(str(old_time), encoding="utf-8")

        deleted = cleanup_expired_memories(mem_dir, now=time.time())
        assert "old-project.md" in deleted
        assert not path.exists()

    def test_keeps_fresh_memories(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="fresh",
            description="Fresh memory",
            content="Recent",
            metadata={"type": "project"},
        ))

        deleted = cleanup_expired_memories(mem_dir, now=time.time())
        assert deleted == []
        assert (mem_dir / "fresh.md").exists()

    def test_respects_custom_ttl(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="short-lived",
            description="Short TTL",
            content="Body",
            metadata={"type": "user"},
        ))
        path = mem_dir / "short-lived.md"
        # Set access time to 3 days ago
        old_time = time.time() - (3 * 86400)
        touch_memory_access(path)
        (mem_dir / ".access-times" / "short-lived.md.atime").write_text(str(old_time), encoding="utf-8")

        # With default TTL (180 days for user), should NOT be deleted
        deleted = cleanup_expired_memories(mem_dir, now=time.time())
        assert deleted == []

        # With custom TTL of 2 days, should be deleted
        deleted = cleanup_expired_memories(
            mem_dir,
            ttl_days={MemoryType.USER: 2},
            now=time.time(),
        )
        assert "short-lived.md" in deleted

    def test_empty_directory(self, mem_dir):
        deleted = cleanup_expired_memories(mem_dir)
        assert deleted == []

    def test_nonexistent_directory(self):
        deleted = cleanup_expired_memories(Path("/nonexistent/path"))
        assert deleted == []

    def test_does_not_delete_memory_md(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="x", description="X", content="Body"))
        index_path = mem_dir / "MEMORY.md"
        assert index_path.exists()

        # Make everything look old
        old_time = time.time() - (365 * 86400)
        os.utime(index_path, (old_time, old_time))

        deleted = cleanup_expired_memories(mem_dir, now=time.time())
        assert "MEMORY.md" not in deleted
        assert index_path.exists()


class TestEvictLeastAccessed:
    def test_evicts_when_over_cap(self, mem_dir):
        store = MemoryStore(mem_dir)
        now = time.time()

        # Create 5 memories with staggered access times
        for i in range(5):
            store.save_entry(MemoryEntry(
                name=f"mem-{i}",
                description=f"Memory {i}",
                content=f"Content {i}",
            ))
            path = mem_dir / f"mem-{i}.md"
            # mem-0 is oldest, mem-4 is newest
            touch_memory_access(path)
            (mem_dir / ".access-times" / f"mem-{i}.md.atime").write_text(
                str(now - (5 - i) * 86400),
                encoding="utf-8",
            )

        # Cap at 3 — should evict the 2 least recently accessed
        evicted = evict_least_accessed(mem_dir, max_memories=3)
        assert len(evicted) == 2
        assert "mem-0.md" in evicted
        assert "mem-1.md" in evicted
        # Most recent should remain
        assert (mem_dir / "mem-4.md").exists()
        assert (mem_dir / "mem-3.md").exists()
        assert (mem_dir / "mem-2.md").exists()

    def test_no_eviction_under_cap(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="a", description="A", content="Body"))
        store.save_entry(MemoryEntry(name="b", description="B", content="Body"))

        evicted = evict_least_accessed(mem_dir, max_memories=10)
        assert evicted == []

    def test_empty_directory(self, mem_dir):
        evicted = evict_least_accessed(mem_dir, max_memories=5)
        assert evicted == []


class TestRunCleanup:
    def test_full_cleanup_pipeline(self, mem_dir):
        store = MemoryStore(mem_dir)
        now = time.time()

        # Create an expired memory
        store.save_entry(MemoryEntry(
            name="expired",
            description="Expired",
            content="Old",
            metadata={"type": "project"},
        ))
        old_time = now - (61 * 86400)
        expired_path = mem_dir / "expired.md"
        touch_memory_access(expired_path)
        (mem_dir / ".access-times" / "expired.md.atime").write_text(str(old_time), encoding="utf-8")

        # Create fresh memories
        store.save_entry(MemoryEntry(
            name="fresh-1", description="Fresh 1", content="Body",
        ))
        store.save_entry(MemoryEntry(
            name="fresh-2", description="Fresh 2", content="Body",
        ))

        stats = run_cleanup(mem_dir, max_memories=200, now=now)
        assert stats.expired_count == 1
        assert stats.evicted_count == 0
        assert stats.remaining_count == 2
        assert "expired.md" in stats.expired_names

    def test_cleanup_refreshes_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        now = time.time()

        store.save_entry(MemoryEntry(
            name="to-expire",
            description="Will expire",
            content="Body",
            metadata={"type": "project"},
        ))
        old_time = now - (61 * 86400)
        expiring = mem_dir / "to-expire.md"
        touch_memory_access(expiring)
        (mem_dir / ".access-times" / "to-expire.md.atime").write_text(str(old_time), encoding="utf-8")

        store.save_entry(MemoryEntry(
            name="keeper", description="Stays", content="Body",
        ))

        run_cleanup(mem_dir, now=now)

        index_content = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "to-expire.md" not in index_content
        assert "keeper.md" in index_content

    def test_no_cleanup_needed(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="fresh", description="Fresh", content="Body",
        ))

        stats = run_cleanup(mem_dir, now=time.time())
        assert stats.expired_count == 0
        assert stats.evicted_count == 0
        assert stats.remaining_count == 1
        assert stats.expired_names is None
        assert stats.evicted_names is None
