"""CRUD operations for individual memory files."""

import os
import tempfile
import time
from pathlib import Path

from .models import MemoryEntry, parse_memory_file, serialize_memory_file, slugify_name


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to a file atomically using tempfile + os.replace."""
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
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
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class MemoryStore:
    """Manage individual memory ``.md`` files in a directory."""

    def __init__(self, memory_dir: Path) -> None:
        self._dir = memory_dir

    @property
    def directory(self) -> Path:
        return self._dir

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_entries(self) -> list[MemoryEntry]:
        """Return all memory entries, excluding MEMORY.md itself."""
        entries: list[MemoryEntry] = []
        for md_file in sorted(self._dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            entry = parse_memory_file(md_file)
            if entry is not None:
                entries.append(entry)
        return entries

    def get_entry(self, name: str) -> MemoryEntry | None:
        """Read a single memory entry by slug name. Returns None if not found."""
        file_path = self._dir / f"{slugify_name(name)}.md"
        if not file_path.exists():
            return None
        return parse_memory_file(file_path)

    def entry_count(self) -> int:
        """Return the number of memory files (excluding MEMORY.md)."""
        return sum(
            1 for p in self._dir.glob("*.md") if p.name != "MEMORY.md"
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_entry(self, entry: MemoryEntry) -> Path:
        """Write a memory entry to disk. Creates parent directories if needed."""
        self._dir.mkdir(parents=True, exist_ok=True)
        file_path = self._resolve_entry_path(entry)
        content = serialize_memory_file(entry)
        _atomic_write_text(file_path, content)
        previous_filename = None
        if entry.file_path is not None:
            previous_filename = entry.file_path.name
        entry.file_path = file_path
        if previous_filename and previous_filename != file_path.name:
            old_path = self._dir / previous_filename
            if old_path.exists():
                old_path.unlink()
            access_dir = self._dir / ".access-times"
            (access_dir / f"{previous_filename}.atime").unlink(missing_ok=True)
            self._remove_index_entry(previous_filename)
        self._update_index_for_entry(entry)
        return file_path

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_entry(self, name: str) -> bool:
        """Delete a memory file by slug name.

        The index is updated before the file is removed. If file deletion then
        fails, a full index rebuild is attempted so MEMORY.md does not keep a
        committed removal for an entry that still exists on disk.
        """
        filename = f"{slugify_name(name)}.md"
        file_path = self._dir / filename
        if not file_path.exists():
            return False
        self._remove_index_entry(filename)
        try:
            file_path.unlink()
        except OSError:
            self.refresh_index()
            raise
        return True

    def refresh_index(self) -> Path:
        """Regenerate MEMORY.md for the current memory directory."""
        from .index import write_memory_index

        return write_memory_index(self._dir)

    def _update_index_for_entry(self, entry: MemoryEntry) -> Path:
        """Incrementally update the index for a single saved entry (atomic)."""
        from .index import update_index_entry, INDEX_FILENAME

        self._dir.mkdir(parents=True, exist_ok=True)
        content = update_index_entry(self._dir, entry)
        index_path = self._dir / INDEX_FILENAME
        _atomic_write_text(index_path, content)
        return index_path

    def _remove_index_entry(self, filename: str) -> Path:
        """Incrementally remove an entry from the index (atomic)."""
        from .index import remove_index_entry, INDEX_FILENAME

        content = remove_index_entry(self._dir, filename)
        index_path = self._dir / INDEX_FILENAME
        _atomic_write_text(index_path, content)
        return index_path

    def _resolve_entry_path(self, entry: MemoryEntry) -> Path:
        if entry.file_path is not None:
            try:
                current = entry.file_path.resolve()
                if current.parent == self._dir.resolve():
                    return self._dir / entry.slug_filename
            except OSError:
                pass
        return self._dir / entry.slug_filename
