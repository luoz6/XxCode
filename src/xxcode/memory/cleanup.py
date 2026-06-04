"""Memory cleanup: TTL expiration and access-frequency-based eviction.

Provides automatic cleanup of stale memories that haven't been accessed
recently, and eviction when the memory count exceeds a configurable cap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .models import MemoryType, parse_memory_file

logger = logging.getLogger(__name__)

_INDEX_FILENAME = "MEMORY.md"
_ACCESS_DIRNAME = ".access-times"

# Access tracking is stored as atime on the file (updated on recall).
# Fallback to mtime if atime is not available.

DEFAULT_TTL_DAYS: dict[MemoryType, int] = {
    MemoryType.USER: 180,
    MemoryType.FEEDBACK: 180,
    MemoryType.PROJECT: 60,
    MemoryType.REFERENCE: 120,
}

DEFAULT_MAX_MEMORIES = 200


@dataclass
class CleanupStats:
    """Result of a cleanup run."""

    expired_count: int = 0
    evicted_count: int = 0
    remaining_count: int = 0
    expired_names: list[str] | None = None
    evicted_names: list[str] | None = None


def _get_last_access_time(path: Path) -> float:
    """Get the last access time of a file in seconds since epoch.

    Uses atime if available and different from mtime (some filesystems
    don't track atime). Falls back to mtime.
    """
    try:
        sidecar = _access_sidecar_path(path)
        if sidecar.exists():
            return float(sidecar.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    try:
        stat = path.stat()
        atime = stat.st_atime
        mtime = stat.st_mtime
        if atime > mtime:
            return atime
        return mtime
    except OSError:
        return 0.0


def touch_memory_access(path: Path) -> None:
    """Update the access time of a memory file (called on recall)."""
    try:
        now = time.time()
        import os
        os.utime(path, (now, path.stat().st_mtime))
        sidecar = _access_sidecar_path(path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(str(now), encoding="utf-8")
    except OSError:
        pass


def cleanup_expired_memories(
    memory_dir: Path,
    *,
    ttl_days: dict[MemoryType, int] | None = None,
    now: float | None = None,
) -> list[str]:
    """Remove memories that have exceeded their TTL since last access.

    Returns a list of deleted filenames.
    """
    if not memory_dir.exists():
        return []

    ttl_map = ttl_days or DEFAULT_TTL_DAYS
    current_time = now or time.time()
    deleted: list[str] = []

    # Collect file stats BEFORE reading content (reading updates atime)
    candidates: list[tuple[Path, float]] = []
    for md_file in memory_dir.glob("*.md"):
        if md_file.name == _INDEX_FILENAME:
            continue
        last_access = _get_last_access_time(md_file)
        candidates.append((md_file, last_access))

    for md_file, last_access in candidates:
        entry = parse_memory_file(md_file)
        if entry is None:
            continue

        memory_type = entry.memory_type
        max_age_seconds = ttl_map.get(memory_type, 180) * 86400

        if current_time - last_access > max_age_seconds:
            try:
                md_file.unlink()
                _access_sidecar_path(md_file).unlink(missing_ok=True)
                deleted.append(md_file.name)
                logger.info("Expired memory: %s (type=%s, age=%.1f days)",
                            md_file.name, memory_type.value,
                            (current_time - last_access) / 86400)
            except OSError:
                logger.debug("Failed to delete expired memory: %s", md_file.name)

    if deleted:
        from .index import write_memory_index
        write_memory_index(memory_dir)

    return deleted


def evict_least_accessed(
    memory_dir: Path,
    *,
    max_memories: int = DEFAULT_MAX_MEMORIES,
) -> list[str]:
    """Evict least-recently-accessed memories when count exceeds the cap.

    Keeps the most recently accessed files up to ``max_memories``.
    Returns a list of evicted filenames.
    """
    if not memory_dir.exists():
        return []

    files: list[tuple[Path, float]] = []
    for md_file in memory_dir.glob("*.md"):
        if md_file.name == _INDEX_FILENAME:
            continue
        access_time = _get_last_access_time(md_file)
        files.append((md_file, access_time))

    if len(files) <= max_memories:
        return []

    # Sort by access time ascending (least recent first)
    files.sort(key=lambda x: x[1])
    to_evict = files[:len(files) - max_memories]

    evicted: list[str] = []
    for path, access_time in to_evict:
        try:
            path.unlink()
            _access_sidecar_path(path).unlink(missing_ok=True)
            evicted.append(path.name)
            logger.info("Evicted memory: %s (last access %.1f days ago)",
                        path.name, (time.time() - access_time) / 86400)
        except OSError:
            logger.debug("Failed to evict memory: %s", path.name)

    if evicted:
        from .index import write_memory_index
        write_memory_index(memory_dir)

    return evicted


def run_cleanup(
    memory_dir: Path,
    *,
    ttl_days: dict[MemoryType, int] | None = None,
    max_memories: int = DEFAULT_MAX_MEMORIES,
    now: float | None = None,
) -> CleanupStats:
    """Run full cleanup: expire by TTL, then evict by access frequency.

    After cleanup, the MEMORY.md index is refreshed.
    """
    expired = cleanup_expired_memories(memory_dir, ttl_days=ttl_days, now=now)
    evicted = evict_least_accessed(memory_dir, max_memories=max_memories)

    if expired or evicted:
        from .index import write_memory_index
        write_memory_index(memory_dir)

    remaining = sum(
        1 for p in memory_dir.glob("*.md") if p.name != _INDEX_FILENAME
    ) if memory_dir.exists() else 0

    return CleanupStats(
        expired_count=len(expired),
        evicted_count=len(evicted),
        remaining_count=remaining,
        expired_names=expired or None,
        evicted_names=evicted or None,
    )


def _access_sidecar_path(path: Path) -> Path:
    return path.parent / _ACCESS_DIRNAME / f"{path.name}.atime"
