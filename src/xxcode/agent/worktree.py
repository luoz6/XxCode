"""Git worktree isolation for sub-agents.

Creates and destroys temporary git worktrees so that parallel agents
operate on independent filesystem snapshots without conflicts.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKTREE_TIMEOUT = 30.0


@dataclass
class WorktreeResult:
    """Outcome of a worktree creation attempt."""

    repo_root: Path
    worktree_path: Path | None  # None when not in a git repo (degraded)
    base_ref: str


class WorktreeManager:
    """Stateless helpers for git worktree lifecycle."""

    @staticmethod
    def find_git_root(cwd: Path) -> Path | None:
        """Locate the repository root, or None if *cwd* is not in a git repo.

        Uses ``git rev-parse --show-toplevel`` synchronously — fast enough
        for the once-per-spawn call site.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                cwd=cwd,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if result.returncode != 0:
            return None
        toplevel = result.stdout.strip()
        return Path(toplevel) if toplevel else None

    @staticmethod
    async def create(
        repo_root: Path,
        base_ref: str = "HEAD",
        *,
        agent_type: str = "agent",
        worktrees_dir: str = ".xxcode/worktrees",
    ) -> WorktreeResult:
        """Create a new git worktree inside *repo_root*.

        The worktree is placed at ``<repo_root>/<worktrees_dir>/<agent_type>-<uuid>/``
        and checked out from *base_ref* (default ``HEAD``).

        Returns a ``WorktreeResult``.  When *repo_root* is not a valid git
        repository the returned ``worktree_path`` is ``None`` (graceful
        degradation).
        """
        worktree_path = (
            repo_root
            / worktrees_dir
            / f"{agent_type}-{uuid.uuid4().hex[:8]}"
        )
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "add",
                str(worktree_path),
                base_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_root),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_WORKTREE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("git worktree add timed out after %ss", _WORKTREE_TIMEOUT)
            return WorktreeResult(repo_root=repo_root, worktree_path=None, base_ref=base_ref)
        except OSError as exc:
            logger.warning("Failed to spawn git for worktree creation: %s", exc)
            return WorktreeResult(repo_root=repo_root, worktree_path=None, base_ref=base_ref)

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
            logger.warning(
                "git worktree add failed (rc=%d): %s",
                proc.returncode,
                stderr_text,
            )
            # Best-effort cleanup of the empty directory that git may have created
            WorktreeManager._rmdir_if_empty(worktree_path)
            return WorktreeResult(repo_root=repo_root, worktree_path=None, base_ref=base_ref)

        logger.info("Created worktree at %s (base=%s)", worktree_path, base_ref)
        return WorktreeResult(
            repo_root=repo_root,
            worktree_path=worktree_path,
            base_ref=base_ref,
        )

    @staticmethod
    async def remove(worktree_path: Path | None) -> None:
        """Remove a worktree and prune its administrative data.

        Idempotent — safe to call multiple times or on an already-deleted path.
        """
        if worktree_path is None:
            return
        if not worktree_path.exists():
            return

        # Determine the repo root from the worktree so we can run
        # `git worktree remove` in the right context.
        repo_root = WorktreeManager.find_git_root(worktree_path)
        if repo_root is None:
            # Not a git worktree — just delete the directory tree.
            _rmtree_force(worktree_path)
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_root),
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_WORKTREE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "git worktree remove timed out for %s — forcing directory removal",
                worktree_path,
            )
            _rmtree_force(worktree_path)
            return
        except OSError:
            _rmtree_force(worktree_path)
            return

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
            logger.debug(
                "git worktree remove returned %d: %s — forcing directory removal",
                proc.returncode,
                stderr_text,
            )

        # Prune stale administrative data and remove any leftover directory.
        await WorktreeManager._prune(repo_root)
        _rmtree_force(worktree_path)

    @staticmethod
    async def _prune(repo_root: Path) -> None:
        """Run ``git worktree prune`` to remove stale administrative entries."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "prune",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_root),
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (asyncio.TimeoutError, OSError):
            pass

    @staticmethod
    def _rmdir_if_empty(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass


def _rmtree_force(path: Path) -> None:
    """Best-effort recursive delete — does not raise on failure."""
    import shutil

    if not path.exists():
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
