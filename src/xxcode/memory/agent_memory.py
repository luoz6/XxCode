"""MEMORY.md indexed memory support for sub-agent types.

Agent memory is separate from the main assistant memory because it stores
agent-type-specific operating knowledge, such as how an explorer agent should
navigate a repository or how a test-runner agent should approach this project.
Like main memory, each scope maintains a MEMORY.md index as its entrypoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_root import find_canonical_git_root
from .index import INDEX_FILENAME, load_memory_index, write_memory_index
from .injection import build_meta_user_message, build_recalled_memories_message
from .cleanup import touch_memory_access
from .models import MemoryType, parse_memory_file
from .recall import MemoryRecall, select_relevant_memories


_UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True)
class AgentMemoryScope:
    """A resolved memory directory for one agent-memory scope."""

    name: str
    path: Path


def sanitize_agent_type_for_path(agent_type: str) -> str:
    """Return a filesystem-safe directory name for an agent type."""
    cleaned = agent_type.strip().lower().replace(":", "-")
    cleaned = _UNSAFE_PATH_CHARS.sub("-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip(".-_")
    return cleaned or "general-purpose"


def resolve_agent_memory_project_root(project_root: Path) -> Path:
    """Return the canonical project root for project/local agent memory scopes."""
    project_root = Path(project_root).resolve()
    git_root = find_canonical_git_root(project_root)
    if git_root is not None:
        return git_root.resolve()
    return project_root


def get_agent_memory_directories(agent_type: str, project_root: Path) -> list[AgentMemoryScope]:
    """Return user, project, and local memory directories for an agent type.

    The directories are returned even if they do not exist; scanners simply
    skip missing paths. Project memory is intended to be shareable, while local
    memory stays machine-specific.
    """
    safe_type = sanitize_agent_type_for_path(agent_type)
    project_root = resolve_agent_memory_project_root(project_root)
    return [
        AgentMemoryScope(
            "user",
            Path.home() / ".XxCode" / "agent-memory" / safe_type,
        ),
        AgentMemoryScope(
            "project",
            project_root / ".xxcode" / "agent-memory" / safe_type,
        ),
        AgentMemoryScope(
            "local",
            project_root / ".xxcode" / "agent-memory-local" / safe_type,
        ),
    ]


def refresh_agent_memory_indexes(agent_type: str, project_root: Path) -> list[AgentMemoryScope]:
    """Refresh MEMORY.md for all existing agent-memory scopes."""
    refreshed: list[AgentMemoryScope] = []
    for scope in get_agent_memory_directories(agent_type, project_root):
        if scope.path.exists():
            write_memory_index(scope.path)
            refreshed.append(scope)
    return refreshed


def build_agent_memory_prompt(agent_type: str, project_root: Path) -> str:
    """Build sub-agent memory behavior instructions for the system prompt."""
    scope_lines: list[str] = []
    for scope in get_agent_memory_directories(agent_type, project_root):
        if not scope.path.exists():
            continue
        _ensure_agent_memory_index(scope.path)
        index_content = load_memory_index(scope.path)
        if not index_content:
            continue
        scope_lines.append(f"- {scope.name} scope: {scope.path}")

    if not scope_lines:
        return ""

    return (
        "\nAgent-type memory:\n"
        f"These memories are specific to the '{agent_type}' sub-agent type. "
        "They describe reusable operating knowledge for this kind of agent, "
        "not user preferences or project decisions.\n"
        "MEMORY.md is the entrypoint index for each agent-memory scope. "
        "The current indexes are provided separately as hidden user context. "
        "Relevant full agent memories may also be recalled automatically as hidden "
        "user context when they closely match the current task. Keep the indexes "
        "in sync when adding or updating agent memories.\n\n"
        "Scopes:\n"
        f"{chr(10).join(scope_lines)}\n"
    )


def build_agent_memory_context_messages(agent_type: str, project_root: Path) -> list[dict]:
    """Build hidden user-context messages for sub-agent ``MEMORY.md`` indexes."""
    messages: list[dict] = []
    for scope in get_agent_memory_directories(agent_type, project_root):
        if not scope.path.exists():
            continue
        _ensure_agent_memory_index(scope.path)
        index_content = load_memory_index(scope.path)
        if not index_content:
            continue
        index_path = scope.path / INDEX_FILENAME
        text = (
            "<system-reminder>\n"
            f"Contents of {index_path} "
            f"({scope.name} agent-memory for '{agent_type}', persists across conversations):\n\n"
            f"{index_content}\n"
            "</system-reminder>"
        )
        messages.append(build_meta_user_message(text, f"agent_memory_{scope.name}"))
    return messages


async def recall_agent_memories_for_query(
    agent_type: str,
    project_root: Path,
    query: str,
    *,
    client_factory,
    recent_tools: list[str] | None = None,
    already_surfaced: set[str] | None = None,
) -> list[MemoryRecall]:
    """Recall relevant full agent-memory files across all scopes for a query."""
    if already_surfaced is None:
        already_surfaced = set()

    manifest_lines: list[str] = []
    file_lookup: dict[str, tuple[Path, str]] = {}
    surfaced_with_scope = {
        item for item in already_surfaced if item.endswith(".md")
    }

    for scope in get_agent_memory_directories(agent_type, project_root):
        if not scope.path.exists():
            continue
        _ensure_agent_memory_index(scope.path)
        index_content = load_memory_index(scope.path)
        if not index_content:
            continue
        for entry in _parse_agent_scope_index(scope.name, index_content):
            scoped_name = entry["scoped_name"]
            manifest_lines.append(
                f"- [indexed] {scoped_name}: [{scope.name}] {entry['description']}"
            )
            file_lookup[scoped_name] = (scope.path / entry["filename"], entry["filename"])

    if not manifest_lines:
        return []

    selected = await select_relevant_memories(
        query=query,
        manifest="\n".join(manifest_lines),
        client_factory=client_factory,
        recent_tools=recent_tools,
        already_surfaced=surfaced_with_scope,
    )
    if not selected:
        return []

    results: list[MemoryRecall] = []
    for scoped_name in selected:
        resolved = file_lookup.get(scoped_name)
        if resolved is None:
            continue
        file_path, filename = resolved
        if not file_path.exists():
            continue
        try:
            full_text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        parsed = parse_memory_file(file_path)
        results.append(
            MemoryRecall(
                filename=filename,
                file_path=file_path,
                content=full_text,
                memory_type=parsed.memory_type if parsed else MemoryType.USER,
                recall_id=scoped_name,
            )
        )
        touch_memory_access(file_path)

    return results


def build_recalled_agent_memories_message(
    recalled: list[MemoryRecall],
    *,
    source: str = "agent_memory_recall",
) -> dict | None:
    """Build a hidden user message for full recalled agent memories."""
    message = build_recalled_memories_message(recalled)
    if message is None:
        return None
    message["metadata"]["source"] = source
    return message


def _ensure_agent_memory_index(scope_path: Path) -> None:
    """Ensure MEMORY.md exists for a scope without rewriting on every read."""
    index_path = scope_path / INDEX_FILENAME
    if not index_path.exists():
        write_memory_index(scope_path)


def _parse_agent_scope_index(scope_name: str, index_content: str) -> list[dict[str, str]]:
    from .index import parse_memory_index

    parsed: list[dict[str, str]] = []
    for entry in parse_memory_index(index_content):
        scoped_filename = f"{scope_name}--{entry.filename}"
        parsed.append(
            {
                "scope": scope_name,
                "filename": entry.filename,
                "description": entry.description,
                "scoped_name": scoped_filename,
            }
        )
    return parsed


__all__ = [
    "AgentMemoryScope",
    "sanitize_agent_type_for_path",
    "resolve_agent_memory_project_root",
    "get_agent_memory_directories",
    "refresh_agent_memory_indexes",
    "build_agent_memory_prompt",
    "build_agent_memory_context_messages",
    "recall_agent_memories_for_query",
    "build_recalled_agent_memories_message",
]
