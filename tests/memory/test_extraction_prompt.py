"""Tests for extraction prompt loading from external file."""

import tempfile
from pathlib import Path

import pytest

from xxcode.memory.extraction import (
    _load_extraction_system_prompt,
    _EXTRACTION_SYSTEM_PROMPT_FALLBACK,
    _EXTRACTION_PROMPT_FILE,
)


class TestLoadExtractionSystemPrompt:
    def test_loads_from_default_file(self):
        """The default prompts/extraction_system.md should exist and load."""
        result = _load_extraction_system_prompt()
        assert len(result) > 100
        assert "memory extraction agent" in result.lower()
        assert "XXCODE.md" in result

    def test_loads_from_custom_path(self, tmp_path):
        custom = tmp_path / "custom_prompt.md"
        custom.write_text("Custom extraction prompt content here.", encoding="utf-8")

        result = _load_extraction_system_prompt(custom_path=custom)
        assert result == "Custom extraction prompt content here."

    def test_custom_path_takes_priority(self, tmp_path):
        custom = tmp_path / "priority.md"
        custom.write_text("I am the priority prompt.", encoding="utf-8")

        result = _load_extraction_system_prompt(custom_path=custom)
        assert result == "I am the priority prompt."

    def test_falls_back_when_custom_path_missing(self):
        result = _load_extraction_system_prompt(
            custom_path=Path("/nonexistent/path/prompt.md")
        )
        # Should fall back to the default file or hardcoded fallback
        assert len(result) > 100
        assert "memory extraction agent" in result.lower()

    def test_falls_back_when_custom_path_empty(self, tmp_path):
        empty_file = tmp_path / "empty.md"
        empty_file.write_text("", encoding="utf-8")

        result = _load_extraction_system_prompt(custom_path=empty_file)
        # Empty file should trigger fallback
        assert len(result) > 100

    def test_fallback_string_is_valid(self):
        assert "What to extract" in _EXTRACTION_SYSTEM_PROMPT_FALLBACK
        assert "What NOT to extract" in _EXTRACTION_SYSTEM_PROMPT_FALLBACK
        assert "Content already in XXCODE.md files" in _EXTRACTION_SYSTEM_PROMPT_FALLBACK
        assert "File format" in _EXTRACTION_SYSTEM_PROMPT_FALLBACK


class TestExtractionPromptFileExists:
    def test_default_prompt_file_exists(self):
        assert _EXTRACTION_PROMPT_FILE.exists(), (
            f"Expected prompt file at {_EXTRACTION_PROMPT_FILE}"
        )

    def test_default_prompt_file_has_content(self):
        content = _EXTRACTION_PROMPT_FILE.read_text(encoding="utf-8")
        assert len(content) > 50
        assert "memory" in content.lower()
