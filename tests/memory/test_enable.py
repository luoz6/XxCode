"""Tests for five-level auto-memory enable check."""

import os

import pytest

from xxcode.memory.enable import is_auto_memory_enabled, _parse_bool_env


class TestParseBoolEnv:
    def test_unset(self):
        assert _parse_bool_env("NONEXISTENT_VAR_12345") is None

    def test_true_values(self):
        for v in ("1", "true", "True", "TRUE", "yes", "YES"):
            os.environ["_TEST_BOOL"] = v
            assert _parse_bool_env("_TEST_BOOL") is True

    def test_false_values(self):
        for v in ("0", "false", "False", "FALSE", "no", "NO"):
            os.environ["_TEST_BOOL"] = v
            assert _parse_bool_env("_TEST_BOOL") is False


class TestIsAutoMemoryEnabled:
    def test_default_enabled(self):
        assert is_auto_memory_enabled() is True

    def test_config_disabled(self):
        assert is_auto_memory_enabled(config_auto_memory_enabled=False) is False

    def test_bare_mode_disabled(self):
        assert is_auto_memory_enabled(bare_mode=True) is False

    def test_remote_mode_disabled(self):
        assert is_auto_memory_enabled(remote_mode=True) is False

    def test_env_var_disabled(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
        assert is_auto_memory_enabled() is False

    def test_env_var_overrides_config(self, monkeypatch):
        """Level 1 (env var) beats Level 4 (config)."""
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
        assert is_auto_memory_enabled(config_auto_memory_enabled=True) is False

    def test_bare_overrides_config(self):
        """Level 2 (bare) beats Level 4 (config)."""
        assert is_auto_memory_enabled(bare_mode=True, config_auto_memory_enabled=True) is False

    def test_env_var_zero_does_not_disable(self, monkeypatch):
        """CLAUDE_CODE_DISABLE_AUTO_MEMORY=0 means 'not set to disable'."""
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
        assert is_auto_memory_enabled() is True
