"""Tests for tool lookup, validation, and execution."""

import asyncio

from xxcode.tools import ToolCall
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.registry import ToolRegistry


def test_registry_execute_runs_valid_tool_call(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    registry = ToolRegistry([ReadFileTool()])
    result = asyncio.run(registry.execute(
        ToolCall(
            id="tool-1",
            name="read_file",
            input={"file_path": str(target), "offset": 1, "limit": 1},
        ),
        {"cwd": str(tmp_path)},
    ))

    assert not result.is_error
    assert result.tool_use_id == "tool-1"
    assert result.content.rstrip("\r") == "2\tbeta"


def test_registry_execute_reports_schema_errors():
    registry = ToolRegistry([ReadFileTool()])

    result = asyncio.run(registry.execute(
        ToolCall(id="tool-2", name="read_file", input={}),
        {},
    ))

    assert result.is_error
    assert "Invalid input" in result.content
    assert "file_path" in result.content


def test_registry_execute_reports_unknown_tools():
    registry = ToolRegistry([ReadFileTool()])

    result = asyncio.run(registry.execute(
        ToolCall(id="tool-3", name="missing_tool", input={}),
        {},
    ))

    assert result.is_error
    assert "Unknown tool" in result.content
    assert "read_file" in result.content
