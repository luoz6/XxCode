"""Unit tests for Tool base class — max_output_chars and format_large_result."""

import asyncio
import tempfile
from pathlib import Path

from xxcode.tools import TOOL_DEFAULTS
from xxcode.tools.base import Tool, build_tool
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.grep_search import GrepSearchTool
from xxcode.tools.BashTool import BashTool
from xxcode.tools.file_edit.tool import EditFileTool


# ── get_max_output_chars ──────────────────────────────────────────────

def test_readfile_max_output_chars():
    tool = ReadFileTool()
    assert tool.get_max_output_chars() == 200_000


def test_grep_max_output_chars():
    tool = GrepSearchTool()
    assert tool.get_max_output_chars() == 100_000


def test_bash_max_output_chars():
    tool = BashTool()
    assert tool.get_max_output_chars() == 100_000


def test_editfile_uses_default_max():
    """EditFileTool has no custom _max_output_chars, should use TOOL_DEFAULTS."""
    tool = EditFileTool()
    assert tool.get_max_output_chars() == TOOL_DEFAULTS["max_output_chars"]
    assert tool.get_max_output_chars() == 50_000


def test_build_tool_overrides_max_output_chars():
    """build_tool() can override max_output_chars at instance level."""
    tool = build_tool(ReadFileTool, max_output_chars=999_000)
    assert tool.get_max_output_chars() == 999_000
    # Class-level should remain unchanged
    assert ReadFileTool().get_max_output_chars() == 200_000


def test_build_tool_preserves_class_limit():
    """build_tool() without max_output_chars arg preserves the class-level limit."""
    tool = build_tool(ReadFileTool)
    assert tool.get_max_output_chars() == 200_000

    tool = build_tool(GrepSearchTool)
    assert tool.get_max_output_chars() == 100_000

    tool = build_tool(EditFileTool)
    assert tool.get_max_output_chars() == 50_000  # TOOL_DEFAULTS fallback


def test_instance_override_takes_priority():
    """Instance-level _max_output_chars takes priority over class-level."""
    tool = ReadFileTool()
    tool._max_output_chars = 500_000
    assert tool.get_max_output_chars() == 500_000


# ── format_large_result ───────────────────────────────────────────────

def test_format_small_result_passes_through():
    tool = ReadFileTool()
    result = asyncio.run(
        tool.format_large_result(
            content="small output",
            max_chars=50_000,
            tool_use_id="test1",
            session_dir=str(Path(tempfile.gettempdir())),
        )
    )
    assert result == "small output"


def test_format_large_result_persists_to_disk():
    tool = ReadFileTool()
    with tempfile.TemporaryDirectory() as tmpdir:
        big_content = "x" * 60_000  # exceeds 50K default
        result = asyncio.run(
            tool.format_large_result(
                content=big_content,
                max_chars=50_000,
                tool_use_id="test-big",
                session_dir=tmpdir,
            )
        )
        # Should have persisted to disk and returned preview
        assert "<persisted-output>" in result
        assert "test-big" in result
        # Check the file exists
        output_file = Path(tmpdir) / "tool-results" / "test-big.txt"
        assert output_file.exists()
        saved = output_file.read_text(encoding="utf-8")
        assert saved == big_content


def test_format_exact_at_limit_returns_as_is():
    tool = ReadFileTool()
    content = "y" * 50_000
    result = asyncio.run(
        tool.format_large_result(
            content=content,
            max_chars=50_000,
            tool_use_id="exact",
            session_dir=str(Path(tempfile.gettempdir())),
        )
    )
    # Exactly at limit should pass through (no need to persist)
    assert result == content
