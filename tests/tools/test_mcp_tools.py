"""Unit tests for MCP tool wrappers — McpTool, ListMcpResourcesTool, ReadMcpResourceTool."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from xxcode.mcp.client import ConnectionState, McpClient, McpError
from xxcode.mcp.config import McpServerConfig
from xxcode.tools.mcp import (
    ListMcpResourcesTool,
    McpTool,
    ReadMcpResourceInput,
    ReadMcpResourceTool,
    build_mcp_input_model,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_mcp_tool(server: str = "test-srv", tool_name: str = "search",
                   schema: dict | None = None) -> McpTool:
    """Create an McpTool pre-configured for a specific MCP tool.

    Uses :func:`build_mcp_input_model` so the input_schema reflects the
    real tool parameters (just like the production registration path).
    """
    if schema is None:
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "Search query"}},
            "required": ["q"],
        }
    safe_name = f"mcp__{server}__{tool_name}"
    input_model = build_mcp_input_model(tool_name, schema)

    tool = McpTool()
    tool.name = safe_name
    tool.input_schema = input_model
    tool.description = "Test MCP tool"
    tool._mcp_server_name = server
    tool._mcp_tool_name = tool_name
    tool._mcp_tool_schema = schema
    return tool


def _make_mcp_context(server: str, client, **extra) -> dict:
    return {
        "mcp_clients": {server: client},
        **extra,
    }


class _FakeMcpClient:
    """Fake McpClient that returns predefined results without real transport."""

    def __init__(self, result: str = "", error: McpError | None = None,
                 resources: list | None = None):
        self._result = result
        self._error = error
        self._resources = resources or []
        self._tool_calls: list[tuple[str, dict]] = []
        self.state = ConnectionState.CONNECTED
        self.server_name = "test-srv"

    async def call_tool(self, name: str, args: dict) -> str:
        self._tool_calls.append((name, args))
        if self._error:
            raise self._error
        return self._result

    async def discover_resources(self):
        if self._error:
            raise self._error
        return self._resources

    async def read_resource(self, uri: str) -> str:
        if self._error:
            raise self._error
        return self._result


# ── build_mcp_input_model ───────────────────────────────────────────────


def test_build_model_required_field():
    model = build_mcp_input_model("test", {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "User name"}},
        "required": ["name"],
    })
    inst = model(name="Alice")
    assert inst.name == "Alice"

    # Missing required field should raise.
    with pytest.raises(Exception):
        model()


def test_build_model_optional_field():
    model = build_mcp_input_model("test", {
        "type": "object",
        "properties": {"verbose": {"type": "boolean"}},
    })
    inst = model()
    assert inst.verbose is None


def test_build_model_multiple_types():
    model = build_mcp_input_model("test", {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "score": {"type": "number"},
            "active": {"type": "boolean"},
        },
        "required": ["count"],
    })
    inst = model(count=3, score=0.95, active=True)
    assert inst.count == 3
    assert inst.score == 0.95
    assert inst.active is True


def test_build_model_empty_properties():
    model = build_mcp_input_model("empty", {"type": "object"})
    # Should return _EmptyInput when there are no properties.
    inst = model()
    assert inst == model()


def test_build_model_sanitizes_name():
    model = build_mcp_input_model("my-tool/with.dots", {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    })
    inst = model(x="ok")
    assert inst.x == "ok"


def test_build_model_nested_object():
    """Nested object should produce a sub-model field."""
    model = build_mcp_input_model("config", {
        "type": "object",
        "properties": {
            "server": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host"],
            },
        },
        "required": ["server"],
    })
    inst = model(server={"host": "localhost", "port": 8080})
    # Nested objects become Pydantic model instances — attribute access.
    assert inst.server.host == "localhost"
    assert inst.server.port == 8080


def test_build_model_array_of_strings():
    model = build_mcp_input_model("tags", {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tags",
            },
        },
        "required": ["tags"],
    })
    inst = model(tags=["a", "b"])
    assert inst.tags == ["a", "b"]


def test_build_model_array_of_objects():
    """Array with object items."""
    model = build_mcp_input_model("items", {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "integer"},
                    },
                    "required": ["key"],
                },
            },
        },
    })
    inst = model(entries=[{"key": "x", "value": 1}])
    # Array items with object schema become Pydantic model instances.
    assert inst.entries[0].key == "x"
    assert inst.entries[0].value == 1


def test_build_model_enum():
    model = build_mcp_input_model("choice", {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["open", "closed", "merged"]},
        },
        "required": ["status"],
    })
    inst = model(status="open")
    assert inst.status == "open"
    # Invalid enum value should be rejected.
    with pytest.raises(Exception):
        model(status="unknown")


def test_build_model_nullable_type_array():
    """type: ["string", "null"] → Optional[str]."""
    model = build_mcp_input_model("n", {
        "type": "object",
        "properties": {
            "nickname": {"type": ["string", "null"], "description": "Optional nickname"},
        },
    })
    inst = model()           # optional → None
    assert inst.nickname is None
    inst2 = model(nickname="zjw")
    assert inst2.nickname == "zjw"


def test_build_model_nullable_integer():
    model = build_mcp_input_model("n", {
        "type": "object",
        "properties": {
            "count": {"type": ["integer", "null"]},
        },
    })
    inst = model(count=3)
    assert inst.count == 3


def test_build_model_constraints():
    model = build_mcp_input_model("range", {
        "type": "object",
        "properties": {
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
            "name": {"type": "string", "minLength": 2, "maxLength": 50},
            "email": {"type": "string", "pattern": r"^.+@.+\..+$"},
        },
    })
    inst = model(age=25, name="Alice", email="alice@example.com")
    assert inst.age == 25
    assert inst.name == "Alice"


def test_build_model_oneof_fallback_to_any():
    """oneOf / anyOf / allOf should fall back to Any type."""
    model = build_mcp_input_model("poly", {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ],
            },
        },
    })
    inst = model(value="hello")
    assert inst.value == "hello"
    inst2 = model(value=42)
    assert inst2.value == 42


def test_build_model_additional_properties_forbid():
    """additionalProperties:false should forbid extra fields."""
    model = build_mcp_input_model("strict", {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    })
    inst = model(name="test")
    assert inst.name == "test"
    # Extra field should be rejected.
    with pytest.raises(Exception):
        model(name="test", extra_field="bad")


def test_build_model_no_type_but_properties():
    """Schema missing 'type' but has 'properties' → inferred as object."""
    model = build_mcp_input_model("inferred", {
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    })
    inst = model(x=1)
    assert inst.x == 1


# ── MCP tool safety defaults ──────────────────────────────────────────


def test_mcp_tool_defaults_to_mutating():
    """External MCP tools must default to NOT read-only and destructive."""
    tool = _make_mcp_tool("external-srv", "create-issue")
    assert tool._is_read_only is False
    assert tool._is_destructive is True
    # The permission pipeline will request confirmation since
    # needs_permission() == not is_read_only() == True.
    assert tool.is_read_only() is False
    assert tool.needs_permission(tool.input_schema(q="test")) is True


def test_builtin_mcp_tools_stay_read_only():
    """Our built-in tools (list/read resources) remain read-only."""
    lister = ListMcpResourcesTool()
    assert lister._is_read_only is True
    assert lister._is_destructive is False

    reader = ReadMcpResourceTool()
    assert reader._is_read_only is True
    assert reader._is_destructive is False


# ── McpTool ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_tool_execute_routes_correctly():
    tool = _make_mcp_tool("my-server", "search")
    client = _FakeMcpClient(result="Found 3 matches")
    context = _make_mcp_context("my-server", client)

    input_model = tool.input_schema(q="test")
    result = await tool.execute(input_model, context)
    assert result == "Found 3 matches"
    assert client._tool_calls == [("search", {"q": "test"})]


@pytest.mark.asyncio
async def test_mcp_tool_execute_server_error():
    tool = _make_mcp_tool("srv", "broken")
    client = _FakeMcpClient(error=McpError(-32603, "Internal error"))
    context = _make_mcp_context("srv", client)

    input_model = tool.input_schema(q="test")
    result = await tool.execute(input_model, context)
    assert "<tool_use_error>" in result
    assert "[-32603]" in result
    assert "Internal error" in result


@pytest.mark.asyncio
async def test_mcp_tool_execute_server_missing():
    tool = _make_mcp_tool("missing-srv", "tool")
    context = {"mcp_clients": {}}

    input_model = tool.input_schema(q="test")
    result = await tool.execute(input_model, context)
    assert "<tool_use_error>" in result
    assert "not connected" in result.lower()


@pytest.mark.asyncio
async def test_mcp_tool_large_output_uses_common_budget(tmp_path):
    tool = _make_mcp_tool("srv", "big-output")
    big_result = "x" * 120_000  # > McpTool._max_output_chars
    client = _FakeMcpClient(result=big_result)

    session_dir = tmp_path / "sessions"
    context = _make_mcp_context(
        "srv",
        client,
        config=type("FakeConfig", (), {"session_dir": str(session_dir)})(),
    )

    input_model = tool.input_schema(q="test")
    result = await tool.execute(input_model, context)
    assert result == big_result

    budgeted = await tool.format_large_result(
        content=result,
        max_chars=tool.get_max_output_chars(),
        tool_use_id="mcp-big",
        session_dir=str(session_dir),
    )
    assert "<persisted-output>" in budgeted
    assert "Output too large" in budgeted

    output_file = session_dir / "tool-results" / "mcp-big.txt"
    assert output_file.exists()
    saved = output_file.read_text(encoding="utf-8")
    assert saved == big_result


def test_mcp_tool_render_tool_use():
    tool = _make_mcp_tool("srv", "search")
    input_model = tool.input_schema(q="hello")
    rendered = tool.render_tool_use(input_model)
    assert "srv/search" in rendered
    assert "q=hello" in rendered


def test_mcp_tool_render_tool_use_no_args():
    """Empty-schema tool should render cleanly."""
    tool = _make_mcp_tool("srv", "ping", schema={"type": "object"})
    input_model = tool.input_schema()
    rendered = tool.render_tool_use(input_model)
    assert "srv/ping" in rendered
    assert "no args" in rendered


# ── ListMcpResourcesTool ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resources_no_clients():
    tool = ListMcpResourcesTool()
    result = await tool.execute(_make_empty_input(), {"mcp_clients": {}})
    assert "No MCP servers" in result


@pytest.mark.asyncio
async def test_list_resources_with_data():
    tool = ListMcpResourcesTool()
    client = _FakeMcpClient(resources=[
        {"uri": "file:///data.csv", "name": "Dataset", "mimeType": "text/csv"},
        {"uri": "file:///config.json", "name": "Configuration"},
    ])
    context = _make_mcp_context("srv", client)

    result = await tool.execute(_make_empty_input(), context)
    assert "Dataset" in result
    assert "file:///data.csv" in result
    assert "text/csv" in result
    assert "file:///config.json" in result


@pytest.mark.asyncio
async def test_list_resources_disconnected_server():
    tool = ListMcpResourcesTool()
    client = _FakeMcpClient()
    client.state = ConnectionState.DISCONNECTED
    context = _make_mcp_context("srv", client)

    result = await tool.execute(_make_empty_input(), context)
    assert "(disconnected)" in result


@pytest.mark.asyncio
async def test_list_resources_server_error():
    tool = ListMcpResourcesTool()
    client = _FakeMcpClient(error=McpError(-32603, "Not supported"))
    context = _make_mcp_context("srv", client)

    result = await tool.execute(_make_empty_input(), context)
    assert "Error listing resources" in result
    assert "Not supported" in result


# ── ReadMcpResourceTool ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_resource_success():
    tool = ReadMcpResourceTool()
    client = _FakeMcpClient(result="file content here")
    context = _make_mcp_context("srv", client)

    result = await tool.execute(
        ReadMcpResourceInput(server_name="srv", uri="file:///test.txt"),
        context,
    )
    assert result == "file content here"


@pytest.mark.asyncio
async def test_read_resource_server_not_found():
    tool = ReadMcpResourceTool()
    context = {"mcp_clients": {}}

    result = await tool.execute(
        ReadMcpResourceInput(server_name="unknown", uri="file:///x.txt"),
        context,
    )
    assert "<tool_use_error>" in result
    assert "not connected" in result.lower()


@pytest.mark.asyncio
async def test_read_resource_mcp_error():
    tool = ReadMcpResourceTool()
    client = _FakeMcpClient(error=McpError(-32002, "Resource not found"))
    context = _make_mcp_context("srv", client)

    result = await tool.execute(
        ReadMcpResourceInput(server_name="srv", uri="file:///missing.txt"),
        context,
    )
    assert "<tool_use_error>" in result
    assert "Resource not found" in result


# ── Helpers ────────────────────────────────────────────────────────────


def _make_empty_input():
    from xxcode.tools.mcp import _EmptyInput
    return _EmptyInput()
