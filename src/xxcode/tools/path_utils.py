"""Shared path resolution and boundary checks for local tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def resolve_tool_path(input_path: str, context: dict[str, Any] | None = None) -> Path:
    """Resolve a tool path relative to the execution context cwd."""
    path = Path(input_path)
    if not path.is_absolute():
        cwd = Path((context or {}).get("cwd") or Path.cwd())
        path = cwd / path
    return path.resolve()


def is_path_within_roots(path: Path, roots: Iterable[str | Path]) -> bool:
    """Return True when ``path`` is equal to or contained by any root."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False

    for raw_root in roots:
        try:
            root = Path(raw_root).resolve()
            resolved.relative_to(root)
            return True
        except (ValueError, OSError, RuntimeError):
            continue
    return False


def check_allowed_read_roots(
    path: Path,
    allowed_roots: Iterable[str | Path] | None,
) -> tuple[bool, str]:
    """Validate that a read target is inside an allowed read root."""
    if not allowed_roots:
        return True, ""
    roots = list(allowed_roots)
    if is_path_within_roots(path, roots):
        return True, ""
    return False, (
        f"Cannot read {path}: outside allowed read roots "
        f"({', '.join(str(root) for root in roots)})."
    )


def check_allowed_write_roots(
    path: Path,
    allowed_roots: Iterable[str | Path] | None,
) -> tuple[bool, str]:
    """Validate that a write target is inside an allowed write root."""
    if not allowed_roots:
        return True, ""
    roots = list(allowed_roots)
    if is_path_within_roots(path, roots):
        return True, ""
    return False, (
        f"Cannot write to {path}: outside allowed roots "
        f"({', '.join(str(root) for root in roots)})."
    )


def is_broad_search_root(path: Path) -> bool:
    """Detect search roots that are too broad to allow by default."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path

    broad = {Path.home().resolve()}
    if resolved.anchor:
        broad.add(Path(resolved.anchor).resolve())
    return resolved in broad or resolved.parent == resolved


def is_sensitive_path(path: Path) -> bool:
    """Heuristic for paths that commonly contain credentials."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path

    home = Path.home().resolve()
    sensitive_dirs = [
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".config",
    ]
    for directory in sensitive_dirs:
        try:
            resolved.relative_to(directory)
            return True
        except ValueError:
            pass

    return resolved.name.lower() in {
        ".env",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "token",
    }
