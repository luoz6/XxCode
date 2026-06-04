"""Unit tests for MCP transport layer — StdioTransport and HttpTransport."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

from xxcode.mcp.protocol import JSONRPCResponse, make_request
from xxcode.mcp.transport import HttpTransport, StdioTransport


# ── Echo server script (used by stdio tests) ───────────────────────────

_ECHO_SERVER = """
import sys, json
while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"echo": req.get("params", {})}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


def _write_echo_server(tmpdir: Path) -> Path:
    """Write a simple JSON-RPC echo server script and return its path."""
    script = tmpdir / "echo_server.py"
    script.write_text(_ECHO_SERVER, encoding="utf-8")
    return script


# ── StdioTransport tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stdio_connect_and_echo():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write_echo_server(Path(tmpdir))
        transport = StdioTransport(sys.executable, [str(script)])
        await transport.connect()
        assert transport.is_connected

        req = make_request("ping", {"key": "value"}, 1)
        await transport.send(req)
        resp = await transport.receive()
        assert isinstance(resp, JSONRPCResponse)
        assert resp.id == 1
        assert resp.result == {"echo": {"key": "value"}}

        await transport.close()
        assert not transport.is_connected


@pytest.mark.asyncio
async def test_stdio_env_passing():
    with tempfile.TemporaryDirectory() as tmpdir:
        server_code = """
import sys, json, os
line = sys.stdin.readline()
req = json.loads(line)
val = os.environ.get("MCP_TEST_VAR", "not-set")
resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"env_val": val}}
sys.stdout.write(json.dumps(resp) + "\\n")
sys.stdout.flush()
"""
        script = Path(tmpdir) / "env_server.py"
        script.write_text(server_code, encoding="utf-8")

        transport = StdioTransport(
            sys.executable, [str(script)],
            env={"MCP_TEST_VAR": "hello-mcp"},
        )
        await transport.connect()

        req = make_request("ping", None, 1)
        await transport.send(req)
        resp = await transport.receive()
        assert isinstance(resp, JSONRPCResponse)
        assert resp.result == {"env_val": "hello-mcp"}

        await transport.close()


@pytest.mark.asyncio
async def test_stdio_stderr_does_not_affect_receive():
    import logging
    del logging
    with tempfile.TemporaryDirectory() as tmpdir:
        server_code = """
import sys, json
sys.stderr.write("diagnostic message\\n")
sys.stderr.flush()
line = sys.stdin.readline()
req = json.loads(line)
resp = {"jsonrpc": "2.0", "id": req["id"], "result": "ok"}
sys.stdout.write(json.dumps(resp) + "\\n")
sys.stdout.flush()
"""
        script = Path(tmpdir) / "stderr_server.py"
        script.write_text(server_code, encoding="utf-8")

        transport = StdioTransport(sys.executable, [str(script)])
        await transport.connect()

        req = make_request("ping", None, 2)
        await transport.send(req)
        resp = await transport.receive()
        assert isinstance(resp, JSONRPCResponse)
        assert resp.result == "ok"

        await asyncio.sleep(0.2)
        await transport.close()


@pytest.mark.asyncio
async def test_stdio_process_exit_returns_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        server_code = """
import sys, json
line = sys.stdin.readline()
req = json.loads(line)
resp = {"jsonrpc": "2.0", "id": req["id"], "result": "done"}
sys.stdout.write(json.dumps(resp) + "\\n")
sys.stdout.flush()
"""
        script = Path(tmpdir) / "exit_server.py"
        script.write_text(server_code, encoding="utf-8")

        transport = StdioTransport(sys.executable, [str(script)])
        await transport.connect()

        req = make_request("ping", None, 3)
        await transport.send(req)
        resp = await transport.receive()
        assert isinstance(resp, JSONRPCResponse)

        resp2 = await transport.receive()
        assert resp2 is None
        assert not transport.is_connected

        await transport.close()


@pytest.mark.asyncio
async def test_stdio_close_kills_process():
    with tempfile.TemporaryDirectory() as tmpdir:
        server_code = """
import sys, time
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(3600)
"""
        script = Path(tmpdir) / "sleep_server.py"
        script.write_text(server_code, encoding="utf-8")

        transport = StdioTransport(sys.executable, [str(script)])
        await transport.connect()
        assert transport.is_connected

        await transport.close()
        assert not transport.is_connected


@pytest.mark.asyncio
async def test_stdio_cwd_respected():
    with tempfile.TemporaryDirectory() as tmpdir:
        server_code = """
import sys, json, os
line = sys.stdin.readline()
req = json.loads(line)
resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"cwd": os.getcwd()}}
sys.stdout.write(json.dumps(resp) + "\\n")
sys.stdout.flush()
"""
        script = Path(tmpdir) / "cwd_server.py"
        script.write_text(server_code, encoding="utf-8")

        transport = StdioTransport(sys.executable, [str(script)], cwd=tmpdir)
        await transport.connect()

        req = make_request("ping", None, 1)
        await transport.send(req)
        resp = await transport.receive()
        assert isinstance(resp, JSONRPCResponse)
        assert resp.result["cwd"] == tmpdir

        await transport.close()


# ── HttpTransport tests ────────────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx.Response stand-in for testing HttpTransport."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError
            raise HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


class _FakeClient:
    """Fake httpx.AsyncClient that records POST calls and returns predefined responses."""

    def __init__(self, responses: list | None = None):
        self.responses = responses or []
        self._calls: list[tuple[str, str]] = []
        self._closed = False

    @property
    def calls(self):
        return self._calls

    @property
    def is_closed(self):
        return self._closed

    async def options(self, url: str):
        return _FakeResponse(200)

    async def post(self, url: str, content: str = ""):
        self._calls.append((url, content))
        resp = self.responses.pop(0) if self.responses else _FakeResponse(200, "{}")
        return resp

    async def aclose(self):
        self._closed = True


@pytest.mark.asyncio
async def test_http_send_receive():
    transport = HttpTransport("https://api.example.com/mcp")
    fake = _FakeClient([
        _FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"status": "ok"}})),
    ])
    transport._client = fake

    req = make_request("tools/list", None, 42)
    await transport.send(req)
    resp = await transport.receive()
    assert isinstance(resp, JSONRPCResponse)
    assert resp.id == 42
    assert resp.result == {"status": "ok"}
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_http_custom_headers():
    transport = HttpTransport("https://api.example.com/mcp", headers={"Authorization": "Bearer xyz"})
    fake = _FakeClient([
        _FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"})),
    ])
    transport._client = fake

    req = make_request("ping", None, 1)
    await transport.send(req)
    await transport.receive()
    assert not fake.is_closed
    await transport.close()
    assert fake.is_closed


@pytest.mark.asyncio
async def test_http_transport_close():
    transport = HttpTransport("https://api.example.com/mcp")
    fake = _FakeClient()
    transport._client = fake

    await transport.close()
    assert fake.is_closed
    assert not transport.is_connected
