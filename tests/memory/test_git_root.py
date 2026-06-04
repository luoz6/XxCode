"""Tests for git root detection with worktree awareness."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from xxcode.memory.git_root import (
    find_canonical_git_root,
    sanitize_git_root_for_path,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, encoding="utf-8",
        timeout=10, cwd=cwd,
    )


@pytest.fixture
def git_repo():
    """Create a real git repo in a temp directory."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        _git(["init", "-b", "main"], cwd=root)
        # Create a commit so the repo is valid
        (root / "README.md").write_text("# Test")
        _git(["config", "user.email", "test@test.com"], cwd=root)
        _git(["config", "user.name", "Test"], cwd=root)
        _git(["add", "."], cwd=root)
        _git(["commit", "-m", "init"], cwd=root)
        yield root


@pytest.fixture
def git_repo_with_subdir(git_repo):
    """Create a subdirectory inside the git repo."""
    subdir = git_repo / "sub" / "deep"
    subdir.mkdir(parents=True)
    yield subdir


class TestFindCanonicalGitRoot:
    def test_repo_root(self, git_repo):
        result = find_canonical_git_root(git_repo)
        assert result is not None
        assert result.resolve() == git_repo.resolve()

    def test_subdir(self, git_repo_with_subdir, git_repo):
        result = find_canonical_git_root(git_repo_with_subdir)
        assert result is not None
        assert result.resolve() == git_repo.resolve()

    def test_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = find_canonical_git_root(Path(tmp))
            assert result is None

    def test_worktree(self, git_repo):
        """Create a worktree and verify both return the main repo root."""
        wt_path = git_repo.parent / "worktree"
        r = _git(["worktree", "add", str(wt_path)], cwd=git_repo)
        if r.returncode != 0:
            pytest.skip("git worktree not supported in this environment")

        try:
            main_result = find_canonical_git_root(git_repo)
            wt_result = find_canonical_git_root(wt_path)
            assert main_result is not None
            assert wt_result is not None
            assert main_result.resolve() == wt_result.resolve()
            assert main_result.resolve() == git_repo.resolve()
        finally:
            _git(["worktree", "remove", "--force", str(wt_path)], cwd=git_repo)
            # Clean up if worktree remove didn't fully clean
            import shutil
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)


class TestSanitizeGitRootForPath:
    def test_deterministic(self):
        a = sanitize_git_root_for_path(Path("/home/user/project"))
        b = sanitize_git_root_for_path(Path("/home/user/project"))
        assert a == b
        assert len(a) == 16

    def test_different_paths_produce_different_hashes(self):
        a = sanitize_git_root_for_path(Path("/path/a"))
        b = sanitize_git_root_for_path(Path("/path/b"))
        assert a != b

    def test_only_hex(self):
        result = sanitize_git_root_for_path(Path("/some/path"))
        assert all(c in "0123456789abcdef" for c in result)
