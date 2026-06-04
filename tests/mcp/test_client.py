"""Unit tests for McpClient — connection lifecycle, tool discovery, invocation."""

import asyncio
import json

import pytest

from xxcode.mcp.client import ConnectionState, McpClient, McpError
from xxcode.mcp.config import McpServerConfig
from xxcode.mcp.content import extract_content
from xxcode.mcp.protocol import make_notification, make_request, serialize_message


# ── Mock Transport ─────────────────────────────────────────────────────


class MockTransport:
    """Controllable transport for testing McpClient without real processes."""

    def __init__(self, responses: list | None = None):
        self.responses = responses or []
        self.sent: list[str] = []
        self._connected = False
        self._closed = False

    async def connect(self):
        self._connected = True

    async def send(self, message):
        self.sent.append(serialize_message(message))

    async def receive(self):
        if not self.responses:
            return None
        raw = self.responses.pop(0)
        if raw is None:
            return None
        from xxcode.mcp.protocol import parse_message
        return parse_message(json.dumps(raw)) if isinstance(raw, dict) else parse_message(raw)

    async def close(self):
        self._closed = True
        self._connected = False

    @property
    def is_connected(self):
        return self._connected and not self._closed


def _make_client(transport: MockTransport | None = None, **kwargs) -> McpClient:
    """Create an McpClient with a mock transport injected."""
    cfg = McpServerConfig(name="test-server", command="echo", **kwargs)
    client = McpClient(_config=cfg)
    if transport is not None:
        client._transport = transport
    return client


def _make_ready_client(
    transport: MockTransport,
    *,
    state: ConnectionState = ConnectionState.CONNECTED,
    request_id: int | None = None,
) -> McpClient:
    client = _make_client(transport=transport)
    client._state = state
    if request_id is not None:
        client._request_id = request_id
    return client


def _init_response(server_name="TestServer", server_version="1.0"):
    """Standard initialize response."""
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2024-11-05",
        "serverInfo": {"name": server_name, "version": server_version},
        "capabilities": {"tools": {}},
    }}


# ── Connection lifecycle ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_initialize_handshake():
    transport = MockTransport([_init_response()])
    client = _make_ready_client(transport, state=ConnectionState.CONNECTING)

    # Manually do the handshake.
    init_req = make_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "XxCode", "version": "0.1.0"},
    }, 1)
    await client._transport.send(init_req)
    resp = await client._transport.receive()

    from xxcode.mcp.protocol import JSONRPCResponse
    assert isinstance(resp, JSONRPCResponse)
    assert resp.result["serverInfo"]["name"] == "TestServer"


def test_client_connect_success():
    client = _make_client()
    # Simulate a completed connect.
    client._state = ConnectionState.CONNECTED
    assert client.state == ConnectionState.CONNECTED


def test_client_connect_failure_sets_error():
    client = _make_client()

    # Directly simulate failed handshake.
    client._state = ConnectionState.ERROR
    client._last_error = "Initialize error: [-32000] Server unavailable"
    assert client.state == ConnectionState.ERROR
    assert "Server unavailable" in client.last_error


@pytest.mark.asyncio
async def test_client_disconnect_clears_state():
    transport = MockTransport()
    client = _make_ready_client(transport)

    await client.disconnect()
    assert client.state == ConnectionState.DISCONNECTED
    assert transport._closed


@pytest.mark.asyncio
async def test_client_reconnect():
    transport = MockTransport()
    client = _make_ready_client(transport, state=ConnectionState.ERROR)

    # Simulate reconnect: disconnect + connect.
    await client.disconnect()
    client._state = ConnectionState.CONNECTED

    assert client.state == ConnectionState.CONNECTED


# ── Tool discovery ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_discover_tools():
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
            {"name": "search", "description": "Search files", "inputSchema": {"type": "object"}},
            {"name": "read", "description": "Read file", "inputSchema": {"type": "object"}},
        ]}},
    ])
    client = _make_ready_client(transport, request_id=1)

    tools = await client.discover_tools()
    assert len(tools) == 2
    assert tools[0]["name"] == "search"
    assert tools[1]["name"] == "read"
    assert len(client.tools) == 2


# ── Tool invocation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_call_tool():
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [
            {"type": "text", "text": "Hello from MCP tool"},
        ]}},
    ])
    client = _make_ready_client(transport, request_id=2)

    result = await client.call_tool("search", {"query": "test"})
    assert result == "Hello from MCP tool"


@pytest.mark.asyncio
async def test_client_call_tool_multi_content():
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 4, "result": {"content": [
            {"type": "text", "text": "Line 1"},
            {"type": "text", "text": "Line 2"},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///data.csv"}},
        ]}},
    ])
    client = _make_ready_client(transport, request_id=3)

    result = await client.call_tool("tool", {})
    assert "Line 1" in result
    assert "Line 2" in result
    assert "[Image:" in result
    assert "file:///data.csv" in result


@pytest.mark.asyncio
async def test_client_call_tool_server_error():
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 5, "error": {"code": -32602, "message": "Invalid params", "data": {"detail": "missing 'query'"}}},
    ])
    client = _make_ready_client(transport, request_id=4)

    with pytest.raises(McpError) as exc_info:
        await client.call_tool("search", {})
    assert exc_info.value.code == -32602
    assert "Invalid params" in exc_info.value.message


@pytest.mark.asyncio
async def test_client_call_tool_when_disconnected():
    client = _make_client()
    client._state = ConnectionState.DISCONNECTED

    with pytest.raises(McpError) as exc_info:
        await client.call_tool("search", {})
    assert "not connected" in exc_info.value.message.lower()


# ── Content extraction ─────────────────────────────────────────────────


def test_extract_content_empty():
    result = extract_content([])
    assert result == ""


def test_extract_content_unknown_type():
    result = extract_content([{"type": "custom", "data": "xyz"}])
    assert "Unknown content type" in result


# ── Resource access ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_read_resource():
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 6, "result": {"contents": [
            {"type": "text", "text": "Resource content here"},
        ]}},
    ])
    client = _make_ready_client(transport, request_id=5)

    result = await client.read_resource("file:///data.txt")
    assert result == "Resource content here"


@pytest.mark.asyncio
async def test_client_read_resource_flat_text():
    """Some servers return flat text instead of contents array."""
    transport = MockTransport([
        {"jsonrpc": "2.0", "id": 7, "result": {"text": "flat text"}},
    ])
    client = _make_ready_client(transport, request_id=6)

    result = await client.read_resource("file:///simple.txt")
    # Flat text fallback
    assert "flat text" in result


# ── Concurrency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_concurrent_requests_serialized():
    """Multiple concurrent _request() calls must not interleave.

    A real StdioTransport would mix response lines if two requests were
    in flight simultaneously.  The _request_lock ensures only one
    coroutine is in the send→receive window at a time.
    """
    call_order: list[int] = []

    class _SpyTransport(MockTransport):
        async def send(self, message):
            # Record that we've entered the critical section.
            call_order.append(1)
            await asyncio.sleep(0.01)  # force yield
            await super().send(message)

        async def receive(self):
            await asyncio.sleep(0.005)
            call_order.append(2)
            return await super().receive()

    transport = _SpyTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "a"}]}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "b"}]}},
    ])
    client = _make_ready_client(transport)

    # Fire two concurrent requests.
    t1 = asyncio.create_task(client.discover_tools())
    t2 = asyncio.create_task(client.discover_tools())

    results = await asyncio.gather(t1, t2)
    assert len(results) == 2

    # With the lock, call_order must be [1,2, 1,2] (each request
    # completes its send→receive before the next starts), NOT
    # [1,1,2,2] (which would indicate interleaving).
    assert call_order in ([1, 2, 1, 2], [1, 2, 1, 2])


@pytest.mark.asyncio
async def test_client_shutdown_disconnects():
    """Verifying that disconnect clears state and closes transport."""
    transport = MockTransport([_init_response()])
    client = _make_ready_client(transport)

    assert client.state == ConnectionState.CONNECTED
    await client.disconnect()
    assert client.state == ConnectionState.DISCONNECTED
    assert transport._closed is True


@pytest.mark.asyncio
async def test_client_disconnect_then_requests_fail():
    """After disconnect, further requests should fail with McpError."""
    transport = MockTransport([_init_response()])
    client = _make_ready_client(transport)

    await client.disconnect()
    # _request checks state at entry
    with pytest.raises(McpError) as exc_info:
        await client.call_tool("x", {})
    assert "not connected" in str(exc_info.value).lower()
