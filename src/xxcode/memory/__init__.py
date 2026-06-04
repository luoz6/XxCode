"""Persistent cross-session memory system.

Four memory types with Markdown + YAML frontmatter storage,
git-worktree-aware path resolution, MEMORY.md entrypoint indexing, semantic
recall, and automatic system prompt guidance.
"""

from .enable import is_auto_memory_enabled
from .git_root import find_canonical_git_root, sanitize_git_root_for_path
from .agent_memory import (
    AgentMemoryScope,
    build_agent_memory_context_messages,
    build_agent_memory_prompt,
    get_agent_memory_directories,
    refresh_agent_memory_indexes,
    sanitize_agent_type_for_path,
)
from .models import MemoryEntry, MemoryType, parse_memory_file, serialize_memory_file, slugify_name
from .index import (
    EntrypointTruncation,
    MemoryIndexEntry,
    generate_memory_index,
    load_memory_index,
    parse_memory_index,
    remove_index_entry,
    truncate_entrypoint_content,
    update_index_entry,
    write_memory_index,
)
from .extraction import (
    ExtractionConfig,
    ExtractionController,
    build_extraction_prompt,
    build_extraction_registry,
)
from .cleanup import (
    CleanupStats,
    cleanup_expired_memories,
    evict_least_accessed,
    run_cleanup,
    touch_memory_access,
    DEFAULT_TTL_DAYS,
    DEFAULT_MAX_MEMORIES,
)
from .recall import (
    MemoryRecall,
    recall_memories_for_query,
    select_relevant_memories,
)
from .resolution import ensure_memory_directory, resolve_memory_directory
from .store import MemoryStore

__all__ = [
    # Types
    "MemoryType",
    "MemoryEntry",
    # Models
    "parse_memory_file",
    "serialize_memory_file",
    "slugify_name",
    # Git root
    "find_canonical_git_root",
    "sanitize_git_root_for_path",
    # Enable
    "is_auto_memory_enabled",
    # Resolution
    "resolve_memory_directory",
    "ensure_memory_directory",
    # Store
    "MemoryStore",
    # Index
    "EntrypointTruncation",
    "MemoryIndexEntry",
    "generate_memory_index",
    "load_memory_index",
    "parse_memory_index",
    "remove_index_entry",
    "truncate_entrypoint_content",
    "update_index_entry",
    "write_memory_index",
    # Agent memory
    "AgentMemoryScope",
    "sanitize_agent_type_for_path",
    "get_agent_memory_directories",
    "refresh_agent_memory_indexes",
    "build_agent_memory_prompt",
    "build_agent_memory_context_messages",
    # Recall
    "MemoryRecall",
    "recall_memories_for_query",
    "select_relevant_memories",
    # Extraction
    "ExtractionConfig",
    "ExtractionController",
    "build_extraction_prompt",
    "build_extraction_registry",
    # Cleanup
    "CleanupStats",
    "cleanup_expired_memories",
    "evict_least_accessed",
    "run_cleanup",
    "touch_memory_access",
    "DEFAULT_TTL_DAYS",
    "DEFAULT_MAX_MEMORIES",
]
