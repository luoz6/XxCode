from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from xxcode.agent.state import AgentState
from xxcode.agent.tools_executor import StreamingToolExecutor
from xxcode.tools import Tool, ToolCall
from xxcode.tools.registry import ToolRegistry


class _LargeReadInput(BaseModel):
    content: str


class _LargeReadTool(Tool):
    name = "large_read"
    description = "large read"
    input_schema = _LargeReadInput

    def is_concurrency_safe(self, validated=None):
        return True

    def is_read_only(self, validated=None):
        return True

    def get_max_output_chars(self):
        return 100_000

    async def format_large_result(self, content, max_chars, tool_use_id, session_dir):
        return content

    def supports_sibling_abort(self):
        return False

    def confirms_file_paths(self):
        return False

    def needs_permission(self, validated):
        return False

    async def execute(self, input: _LargeReadInput, context):
        return input.content


@pytest.mark.asyncio
async def test_remaining_results_apply_aggregate_budget_in_fifo_order(tmp_path):
    registry = ToolRegistry()
    registry.register(_LargeReadTool())
    config = SimpleNamespace(
        session_dir=tmp_path / "sessions",
        max_message_tool_results_chars=120,
    )
    state = AgentState()
    executor = StreamingToolExecutor(registry, config, state, context={})

    executor.add_tool(ToolCall(id="tool-1", name="large_read", input={"content": "a" * 100}))
    executor.add_tool(ToolCall(id="tool-2", name="large_read", input={"content": "b" * 100}))

    results = await executor.get_remaining_results()

    assert [result["tool_use_id"] for result in results] == ["tool-1", "tool-2"]
    assert sum(len(result["content"]) for result in results) <= 120
