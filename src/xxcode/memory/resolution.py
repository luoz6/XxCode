"""Three-level priority path resolution for the memory directory."""

import os
from pathlib import Path

from .git_root import find_canonical_git_root, sanitize_git_root_for_path


def resolve_memory_directory(
    config_cwd: Path,
    auto_memory_directory: str | None = None,
) -> Path | None:
    """Resolve the memory directory using three-level priority.

    1. ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`` env var  — bypasses all other logic
    2. ``auto_memory_directory`` setting                 — from user/managed settings only
    3. Default: ``~/.XxCode/projects/{git-root-hash}/memory/``

    Returns None when not in a git repo and no override is set.
    """
    # Level 1: environment override
    env_override = os.environ.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")
    if env_override:
        return Path(env_override.strip())

    # Level 2: settings-configured directory
    if auto_memory_directory:
        p = Path(auto_memory_directory)
        if not p.is_absolute():
            p = config_cwd / p
        return p.resolve()

    # Level 3: default path based on canonical git root
    git_root = find_canonical_git_root(config_cwd)
    if git_root is None:
        return None

    project_hash = sanitize_git_root_for_path(git_root)
    return Path.home() / ".XxCode" / "projects" / project_hash / "memory"


def ensure_memory_directory(path: Path) -> Path:
    """Create the memory directory (and parents) if it doesn't exist.

    Idempotent — safe to call even when the directory already exists.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path
