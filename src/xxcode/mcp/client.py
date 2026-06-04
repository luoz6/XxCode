"""MCP client lifecycle, discovery, and invocation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from .config import McpServerConfig
from .content import extract_content
from .protocol import (
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCResponse,
    make_notification,
    make_request,
)
from .transport import HttpTransport, McpTransport, StdioTransport

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "XxCode"
CLIENT_VERSION = "0.1.0"
DEFAULT_PING_INTERVAL = 30.0
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    ERROR = auto()


class McpError(Exception):
    """Error returned by an MCP server."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        if data is not None:
            super().__init__(f"[{code}] {message} (data: {data})")
        else:
            super().__init__(f"[{code}] {message}")


@dataclass
class McpClient:
    """Manages one MCP server connection."""

    _config: McpServerConfig
    _transport: McpTransport | None = field(default=None, repr=False)
    _state: ConnectionState = field(default=ConnectionState.DISCONNECTED, repr=False)
    _request_id: int = field(default=0, repr=False)
    _server_info: dict[str, Any] = field(default_factory=dict, repr=False)
    _server_capabilities: dict[str, Any] = field(default_factory=dict, repr=False)
    _tools: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _resources: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _last_error: str | None = field(default=None, repr=False)
    _ping_task: asyncio.Task | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _request_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    @property
    def server_name(self) -> str:
        return self._config.name

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def connect(self) -> bool:
        """Establish transport, perform initialize handshake, and start ping."""
        async with self._lock:
            if self._state == ConnectionState.CONNECTED:
                return True

            self._state = ConnectionState.CONNECTING
            self._last_error = None

            try:
                self._transport = self._create_transport()
            except Exception as exc:
                self._last_error = f"Failed to create transport: {exc}"
                self._state = ConnectionState.ERROR
                return False

            if not await self._connect_transport():
                return False
            if not await self._initialize():
                return False

            self._state = ConnectionState.CONNECTED
            logger.info(
                "Connected to MCP server '%s' (%s %s)",
                self._config.name,
                self._server_info.get("name", "unknown"),
                self._server_info.get("version", ""),
            )
            self._start_ping()
            return True

    async def disconnect(self) -> None:
        """Gracefully close the connection."""
        async with self._lock:
            self._stop_ping()
            if self._transport is not None:
                try:
                    await self._transport.close()
                except Exception:
                    pass
                self._transport = None
            self._state = ConnectionState.DISCONNECTED

    async def reconnect(self) -> bool:
        """Disconnect and reconnect."""
        await self.disconnect()
        return await self.connect()

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Call tools/list and cache the result."""
        resp = await self._request("tools/list", {})
        tools = resp.get("tools", [])
        self._tools = tools
        return list(tools)

    async def discover_resources(self) -> list[dict[str, Any]]:
        """Call resources/list and cache the result."""
        resp = await self._request("resources/list", {})
        resources = resp.get("resources", [])
        self._resources = resources
        return list(resources)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return extracted text content."""
        resp = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return extract_content(resp.get("content", []))

    async def read_resource(self, uri: str) -> str:
        """Call resources/read and return extracted text content."""
        resp = await self._request("resources/read", {"uri": uri})
        contents = resp.get("contents", [])
        if isinstance(contents, list) and contents:
            return extract_content(contents)
        if isinstance(resp, dict) and "text" in resp:
            return resp["text"]
        return str(resp)

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one JSON-RPC request and wait for its matching response.

        Requests are serialized per client so concurrent calls cannot interleave
        reads on a shared stdio transport.
        """
        async with self._request_lock:
            if self._state != ConnectionState.CONNECTED:
                raise McpError(
                    -32002,
                    f"Server '{self._config.name}' is not connected (state: {self._state.name})",
                )
            if self._transport is None:
                raise McpError(-32002, "Transport not available")

            req_id = self._next_id()
            req = make_request(method, params, req_id)
            try:
                await self._transport.send(req)
            except Exception as exc:
                self._state = ConnectionState.ERROR
                self._last_error = f"Send failed: {exc}"
                raise McpError(-32002, f"Failed to send request: {exc}")

            try:
                while True:
                    msg = await asyncio.wait_for(
                        self._transport.receive(),
                        timeout=DEFAULT_REQUEST_TIMEOUT,
                    )
                    if msg is None:
                        self._state = ConnectionState.ERROR
                        self._last_error = "Server closed connection"
                        raise McpError(-32002, "Server closed connection unexpectedly")

                    if isinstance(msg, JSONRPCNotification):
                        continue

                    if isinstance(msg, (JSONRPCResponse, JSONRPCError)):
                        if msg.id == req_id:
                            if isinstance(msg, JSONRPCError):
                                err = msg.error
                                raise McpError(
                                    err.get("code", -32001),
                                    err.get("message", "Unknown error"),
                                    err.get("data"),
                                )
                            return msg.result

                    logger.debug(
                        "Received response for id=%s, waiting for id=%s",
                        getattr(msg, "id", "?"),
                        req_id,
                    )
            except asyncio.TimeoutError:
                self._state = ConnectionState.ERROR
                self._last_error = f"Request '{method}' timed out after {DEFAULT_REQUEST_TIMEOUT}s"
                raise McpError(-32002, self._last_error)

    def _create_transport(self) -> McpTransport:
        if self._config.is_stdio():
            return StdioTransport(
                command=self._config.command or "",
                args=self._config.args,
                env=self._config.env,
                cwd=self._config.cwd,
            )
        if self._config.is_http():
            return HttpTransport(
                url=self._config.url or "",
                headers=self._config.headers,
            )
        raise ValueError("No transport configured (need command or url)")

    async def _connect_transport(self) -> bool:
        if self._transport is None:
            self._last_error = "Transport not available"
            self._state = ConnectionState.ERROR
            return False

        try:
            await asyncio.wait_for(
                self._transport.connect(),
                timeout=DEFAULT_CONNECT_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            self._last_error = "Transport connect timed out"
        except Exception as exc:
            self._last_error = f"Transport connect failed: {exc}"
        self._state = ConnectionState.ERROR
        return False

    async def _initialize(self) -> bool:
        if self._transport is None:
            self._last_error = "Transport not available"
            self._state = ConnectionState.ERROR
            return False

        try:
            init_req = make_request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                self._next_id(),
            )
            await self._transport.send(init_req)
            resp = await asyncio.wait_for(
                self._transport.receive(),
                timeout=DEFAULT_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._last_error = "Initialize handshake timed out"
            self._state = ConnectionState.ERROR
            return False
        except Exception as exc:
            self._last_error = f"Initialize handshake failed: {exc}"
            self._state = ConnectionState.ERROR
            return False

        if resp is None:
            self._last_error = "Server closed connection during initialize"
            self._state = ConnectionState.ERROR
            return False
        if isinstance(resp, JSONRPCError):
            self._last_error = (
                f"Initialize error: [{resp.error.get('code')}] "
                f"{resp.error.get('message')}"
            )
            self._state = ConnectionState.ERROR
            return False
        if not isinstance(resp, JSONRPCResponse):
            self._last_error = (
                f"Unexpected response type during initialize: {type(resp).__name__}"
            )
            self._state = ConnectionState.ERROR
            return False

        result = resp.result or {}
        self._server_info = result.get("serverInfo", {})
        self._server_capabilities = result.get("capabilities", {})

        try:
            await self._transport.send(make_notification("notifications/initialized"))
        except Exception:
            logger.debug("Failed to send initialized notification")

        return True

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _start_ping(self) -> None:
        if self._ping_task is not None and not self._ping_task.done():
            return
        self._ping_task = asyncio.create_task(self._ping_loop())

    def _stop_ping(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None

    async def _ping_loop(self, interval: float = DEFAULT_PING_INTERVAL) -> None:
        """Periodic ping to keep the connection alive."""
        while self._state == ConnectionState.CONNECTED:
            await asyncio.sleep(interval)
            if self._state != ConnectionState.CONNECTED:
                break
            try:
                await self._request("ping", {})
            except McpError:
                logger.warning("Ping failed for MCP server '%s'", self._config.name)
                self._state = ConnectionState.ERROR
                self._last_error = "Ping failed - connection lost"
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Ping error for MCP server '%s': %s", self._config.name, exc)
                break


__all__ = ["McpClient", "ConnectionState", "McpError"]
