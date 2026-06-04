"""Tests for three-level memory directory resolution."""

import os
import tempfile
from pathlib import Path

import pytest

import xxcode.memory.resolution as resolution
from xxcode.memory.resolution import ensure_memory_directory, resolve_memory_directory


class TestResolveMemoryDirectory:
    def test_env_override(self, monkeypatch):
        override_path = "/custom/memory/path"
        monkeypatch.setenv("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", override_path)

        result = resolve_memory_directory(config_cwd=Path.cwd())
        assert result == Path(override_path)

    def test_setting_override(self):
        result = resolve_memory_directory(
            config_cwd=Path.cwd(),
            auto_memory_directory="/settings/memory/path",
        )
        assert result == Path("/settings/memory/path").resolve()

    def test_env_beats_setting(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", "/env/path")

        result = resolve_memory_directory(
            config_cwd=Path.cwd(),
            auto_memory_directory="/settings/path",
        )
        assert result == Path("/env/path")

    def test_non_git_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = resolve_memory_directory(config_cwd=Path(tmp))
            assert result is None

    def test_env_override_bypasses_git_check(self, monkeypatch):
        """Even in a non-git dir, env override should work."""
        monkeypatch.setenv("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", "/forced/path")
        with tempfile.TemporaryDirectory() as tmp:
            result = resolve_memory_directory(config_cwd=Path(tmp))
            assert result == Path("/forced/path")

    def test_trim_env_value(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", "  /padded/path  ")
        result = resolve_memory_directory(config_cwd=Path.cwd())
        assert result == Path("/padded/path")

    def test_default_uses_xxcode_project_memory_dir(self, monkeypatch, tmp_path):
        git_root = tmp_path / "repo"
        git_root.mkdir()
        monkeypatch.setattr(resolution, "find_canonical_git_root", lambda cwd: git_root)
        monkeypatch.setattr(resolution, "sanitize_git_root_for_path", lambda root: "abc123")

        result = resolve_memory_directory(config_cwd=git_root)

        assert result == Path.home() / ".XxCode" / "projects" / "abc123" / "memory"


class TestEnsureMemoryDirectory:
    def test_creates_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "deep" / "nested" / "memory"
            result = ensure_memory_directory(mem_dir)
            assert result == mem_dir
            assert mem_dir.exists()
            assert mem_dir.is_dir()

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "memory"
            ensure_memory_directory(mem_dir)
            # Second call should not raise
            result = ensure_memory_directory(mem_dir)
            assert result == mem_dir
            assert mem_dir.exists()
