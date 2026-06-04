"""Integration tests for MCP subsystem.

Coverage:
  - register_mcp_tools() end-to-end (config -> connect -> discover -> register)
  - connect failure does not pollute registry
  - no-config clean no-op
  - shutdown / clear_mcp lifecycle
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from xxcode.mcp.client import ConnectionState, McpClient
from xxcode.mcp.config import McpServerConfig, load_project_mcp_config
from xxcode.tools.registry import ToolRegistry


def _write_config(tmp_path: Path, servers: dict) -> Path:
    """Write a .mcp.json file and return the project root."""
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return tmp_path


class _FakeTransport:
    """Controllable transport stub."""

    def __init__(self, tools_response=None):
        self._connected = False
        self._closed = False
        self._init_done = False
        self._tools_response = tools_response or {"tools": []}
        self._sent: list = []
        self._recv_queue: list = []

    async def connect(self):
        self._connected = True

    async def send(self, msg):
        self._sent.append(msg)
        if not self._init_done:
            self._init_done = True
            self._recv_queue.append({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "serverInfo": {"name": "fake", "version": "1.0"},
                    "capabilities": {},
                },
            })
        elif msg.method == "tools/list":
            self._recv_queue.append({
                "jsonrpc": "2.0",
                "id": msg.id,
                "result": self._tools_response,
            })

    async def receive(self):
        if not self._recv_queue:
            return None
        raw = self._recv_queue.pop(0)
        from xxcode.mcp.protocol import parse_message

        return parse_message(json.dumps(raw))

    async def close(self):
        self._closed = True
        self._connected = False

    @property
    def is_connected(self):
        return self._connected and not self._closed


def _make_connected_client(name="fake-srv", tools=None) -> McpClient:
    """Create a connected McpClient with a fake transport and known tools."""
    client = McpClient(_config=McpServerConfig(name=name, command="fake"))
    transport = _FakeTransport(tools_response={
        "tools": tools
        or [
            {
                "name": "search",
                "description": "Search",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        ],
    })
    client._transport = transport
    client._state = ConnectionState.CONNECTED
    transport._connected = True
    return client


@pytest.mark.asyncio
async def test_register_mcp_tools_e2e():
    """End-to-end: config -> connect -> discover -> register deferred tools."""
    from xxcode.tools.mcp.integration import register_mcp_tools

    with tempfile.TemporaryDirectory() as td:
        root = _write_config(Path(td), {
            "test-server": {
                "command": "fake-cmd",
                "args": ["--fake"],
            },
        })

        registry = ToolRegistry()
        cfg = load_project_mcp_config(root)[0]
        context: dict = {
            "trusted_mcp_servers": {
                cfg.name: cfg.fingerprint(),
            }
        }

        with patch.object(McpClient, "connect", return_value=True):
            with patch.object(
                McpClient,
                "discover_tools",
                return_value=[
                    {
                        "name": "search",
                        "description": "Search things",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search term",
                                }
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "create",
                        "description": "Create item",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                            "required": ["title"],
                        },
                    },
                ],
            ):
                await register_mcp_tools(registry, root, context)

        assert "mcp_clients" in context
        assert "test-server" in context["mcp_clients"]
        assert context["mcp_clients"]["test-server"] is not None

        deferred = registry.get_deferred_tools()
        assert "mcp__test_server__search" in deferred
        assert "mcp__test_server__create" in deferred

        search_tool = registry.activate_tool("mcp__test_server__search")
        assert search_tool is not None
        assert search_tool._mcp_server_name == "test-server"
        assert search_tool._mcp_tool_name == "search"

        input_model = search_tool.input_schema(query="hello")
        assert input_model.query == "hello"

        active_names = [tool.name for tool in registry.list_tools()]
        assert "mcp__test_server__search" in active_names


@pytest.mark.asyncio
async def test_register_mcp_tools_project_config_requires_trust():
    from xxcode.tools.mcp.integration import register_mcp_tools

    with tempfile.TemporaryDirectory() as td:
        root = _write_config(Path(td), {
            "test-server": {"command": "fake-cmd"},
        })

        registry = ToolRegistry()
        context: dict = {}

        with patch.object(McpClient, "connect", return_value=True):
            await register_mcp_tools(registry, root, context)

        assert context.get("mcp_clients", {}) == {}
        assert registry.get_deferred_tools() == {}
        assert context.get("pending_mcp_trust")


@pytest.mark.asyncio
async def test_register_mcp_tools_connect_failure_no_pollution():
    """A server that fails to connect should not leave side effects."""
    from xxcode.tools.mcp.integration import register_mcp_tools

    with tempfile.TemporaryDirectory() as td:
        root = _write_config(Path(td), {
            "bad-server": {"command": "does-not-exist"},
        })

        registry = ToolRegistry()
        context: dict = {}

        with patch.object(McpClient, "connect", return_value=False):
            await register_mcp_tools(registry, root, context)

        assert context.get("mcp_clients", {}) == {}
        assert registry.get_deferred_tools() == {}


@pytest.mark.asyncio
async def test_register_mcp_tools_no_config():
    """No config file -> clean no-op."""
    from xxcode.tools.mcp.integration import register_mcp_tools

    with tempfile.TemporaryDirectory() as td:
        registry = ToolRegistry()
        context: dict = {}
        await register_mcp_tools(registry, Path(td), context)

        assert context.get("mcp_clients", {}) == {}
        assert registry.get_deferred_tools() == {}


@pytest.mark.asyncio
async def test_disconnect_mcp_clears_context():
    """CoreExecutionEngine._disconnect_mcp() disconnects all clients."""
    from xxcode.agent.loop import CoreExecutionEngine

    engine = CoreExecutionEngine()

    client = _make_connected_client("srv-a")
    engine._context["mcp_clients"] = {"srv-a": client}
    assert client._transport._connected is True

    await engine._disconnect_mcp()

    assert client.state == ConnectionState.DISCONNECTED
    assert client._transport is None
    assert engine._context.get("mcp_clients", {}) == {}


@pytest.mark.asyncio
async def test_shutdown_resets_mcp_state():
    """shutdown() calls _disconnect_mcp + resets flags."""
    from xxcode.agent.loop import CoreExecutionEngine

    engine = CoreExecutionEngine()
    engine._mcp_initialized = True
    engine._aborted = True

    client = _make_connected_client("srv-b")
    engine._context["mcp_clients"] = {"srv-b": client}

    await engine.shutdown()

    assert client.state == ConnectionState.DISCONNECTED
    assert engine._mcp_initialized is False
    assert engine._aborted is False
    assert engine._context.get("mcp_clients", {}) == {}


@pytest.mark.asyncio
async def test_clear_mcp_allows_reinit():
    """clear_mcp() disconnects and resets _mcp_initialized."""
    from xxcode.agent.loop import CoreExecutionEngine

    engine = CoreExecutionEngine()
    engine._mcp_initialized = True

    client = _make_connected_client("srv-c")
    engine._context["mcp_clients"] = {"srv-c": client}

    await engine.clear_mcp()

    assert client.state == ConnectionState.DISCONNECTED
    assert engine._mcp_initialized is False
