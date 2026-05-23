"""Context collection: git status, CLAUDE.md loading, environment info."""

import subprocess
import os
from pathlib import Path


def get_git_context(cwd: Path, timeout: float = 3.0) -> str:
    """Collect git context: branch, recent commits, staged status.

    Gracefully degrades if not in a git repo or on timeout.
    """
    lines: list[str] = []

    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        if branch.returncode == 0 and branch.stdout.strip():
            lines.append(f"Git branch: {branch.stdout.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        if log.returncode == 0 and log.stdout.strip():
            lines.append("Recent commits:")
            for commit_line in log.stdout.strip().split("\n"):
                lines.append(f"  {commit_line}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        if status.returncode == 0 and status.stdout.strip():
            lines.append("Working tree status:")
            for status_line in status.stdout.strip().split("\n")[:20]:
                lines.append(f"  {status_line}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    if not lines:
        return ""

    return "\n".join(lines)


def load_claude_md(cwd: Path) -> str:
    """Walk up directory tree collecting CLAUDE.md files.

    Ancestor directories come first, children after — later entries override earlier.
    """
    parts: list[str] = []
    current = cwd.resolve()

    while True:
        md_file = current / "CLAUDE.md"
        if md_file.exists():
            try:
                parts.insert(0, md_file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass

        parent = current.parent
        if parent == current:
            break
        current = parent

    return "\n\n---\n\n".join(parts) if parts else ""


def get_environment_info() -> dict[str, str]:
    """Collect runtime environment information."""
    import platform
    import sys

    return {
        "cwd": str(Path.cwd()),
        "platform": platform.platform(),
        "shell": os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown")),
        "python_version": sys.version.split()[0],
    }
