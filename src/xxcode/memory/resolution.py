"""Three-level priority path resolution for the memory directory."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .git_root import find_canonical_git_root, sanitize_git_root_for_path


def _sanitize_path_component(component: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", component)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip(".-_")


def sanitize_path_for_path(path: Path) -> str:
    """Produce a readable, stable slug for a filesystem path."""
    candidate = Path(path).expanduser()
    raw = str(candidate)

    if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:(?:[\\/]|$)", raw):
        raw = str(candidate.resolve())

    raw = raw.replace("\\", "/").strip()

    drive = ""
    if re.match(r"^[A-Za-z]:(?:/|$)", raw):
        drive = raw[0]
        raw = raw[2:]

    raw = raw.lstrip("/")
    segments = [segment for segment in raw.split("/") if segment and segment != "."]
    slug_parts = [_sanitize_path_component(segment) for segment in segments]
    slug_parts = [part for part in slug_parts if part]
    body = "-".join(slug_parts) or "root"

    if drive:
        return f"{drive}--{body}"
    return body


def resolve_memory_directory(
    config_cwd: Path,
    auto_memory_directory: str | None = None,
) -> Path | None:
    """Resolve the memory directory using three-level priority.

    1. ``CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`` env var - bypasses all other logic
    2. ``auto_memory_directory`` setting - from user/managed settings only
    3. Default: ``~/.XxCode/projects/{git-root-hash}/memory/`` in git repos,
       or ``~/.XxCode/projects/{path-slug}/memory/`` outside git repos.
    """
    env_override = os.environ.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")
    if env_override:
        return Path(env_override.strip())

    if auto_memory_directory:
        p = Path(auto_memory_directory)
        if not p.is_absolute():
            p = config_cwd / p
        return p.resolve()

    git_root = find_canonical_git_root(config_cwd)
    if git_root is None:
        project_key = sanitize_path_for_path(config_cwd)
    else:
        project_key = sanitize_git_root_for_path(git_root)

    return Path.home() / ".XxCode" / "projects" / project_key / "memory"


def ensure_memory_directory(path: Path) -> Path:
    """Create the memory directory (and parents) if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)
    return path
