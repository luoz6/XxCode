"""Find the canonical git root, with worktree awareness.

All worktrees of the same repository share one memory directory by resolving
to the main repository's root (not the worktree's root).
"""

import hashlib
import subprocess
from pathlib import Path


def _run_git(args: list[str], cwd: Path, timeout: float = 3.0) -> str | None:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip()


def find_canonical_git_root(cwd: Path) -> Path | None:
    """Return the canonical git root for the given working directory.

    For normal repos, this is the output of ``git rev-parse --show-toplevel``.
    For worktrees, the ``.git`` file is parsed to locate the main repo root,
    so all worktrees share the same memory directory.

    Returns None if not inside a git repository.
    """
    # Let git tell us the toplevel
    toplevel = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if toplevel:
        top = Path(toplevel)
        git_entry = top / ".git"
        if git_entry.is_file():
            # Worktree: .git is a file pointing to main repo's .git/worktrees/name
            git_dir = _parse_worktree_gitdir(git_entry)
            if git_dir:
                # git_dir is .../main/.git/worktrees/<name> → parent ×3 = .../main
                return git_dir.resolve().parent.parent.parent
        return top

    # Fallback: walk up looking for .git (git not available)
    current = cwd.resolve()
    while True:
        git_entry = current / ".git"
        if git_entry.exists():
            if git_entry.is_file():
                git_dir = _parse_worktree_gitdir(git_entry)
                if git_dir:
                    return git_dir.resolve().parent.parent.parent
                return current
            return current

        parent = current.parent
        if parent == current:
            return None
        current = parent


def _parse_worktree_gitdir(git_file: Path) -> Path | None:
    """Parse a worktree .git file to find the main repo's .git directory.

    Format::

        gitdir: /path/to/main/.git/worktrees/name
    """
    try:
        content = git_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in content.splitlines():
        if line.startswith("gitdir:"):
            gitdir_path = line.removeprefix("gitdir:").strip()
            return Path(gitdir_path)
    return None


def sanitize_git_root_for_path(git_root: Path) -> str:
    """Produce a short, stable hash from a git root path.

    Uses SHA256 for collision resistance, returning the first 16 hex chars.
    """
    path_str = str(git_root.resolve())
    digest = hashlib.sha256(path_str.encode("utf-8")).hexdigest()
    return digest[:16]
