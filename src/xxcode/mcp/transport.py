"""Transport abstraction for MCP JSON-RPC message exchange.

Two transport implementations:

  StdioTransport — spawns a child process, communicates via stdin/stdout
                    with newline-delimited JSON messages (one per line).
                    Used for local MCP servers (node, python, etc.).

  HttpTransport   — sends JSON-RPC requests via HTTP POST and reads the
                    JSON response body. Suitable for remote MCP servers.
                    Uses the project's existing httpx dependency.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .protocol import (
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    parse_message,
    serialize_message,
)

logger = logging.getLogger(__name__)


# ── Abstract transport ─────────────────────────────────────────────────


class McpTransport(ABC):
    """Abstract transport for JSON-RPC message exchange with an MCP server."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the transport connection."""
        ...

    @abstractmethod
    async def send(
        self,
        message: JSONRPCRequest | JSONRPCNotification,
    ) -> None:
        """Send a JSON-RPC message to the server."""
        ...

    @abstractmethod
    async def receive(
        self,
    ) -> JSONRPCResponse | JSONRPCError | JSONRPCNotification | None:
        """Receive the next JSON-RPC message from the server.

        Returns None when the transport is closed or EOF is reached.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the transport and release resources."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the transport is currently open and operational."""
        ...


# ── Stdio transport ────────────────────────────────────────────────────


class StdioTransport(McpTransport):
    """Transport via child process stdin/stdout with newline-delimited JSON."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self._command = command
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._buffer = ""

    async def connect(self) -> None:
        import os
        import platform

        safe_keys = {
            "PATH",
            "HOME",
            "USER",
            "USERNAME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "TEMP",
            "TMP",
        }
        if platform.system() == "Windows":
            safe_keys.add("SYSTEMROOT")
        else:
            safe_keys.add("SHELL")
        merged_env = {
            key: value
            for key, value in os.environ.items()
            if key in safe_keys
        }
        if self._env:
            merged_env.update(self._env)

        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=self._cwd,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._buffer = ""

    async def send(
        self,
        message: JSONRPCRequest | JSONRPCNotification,
    ) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("StdioTransport not connected")
        data = serialize_message(message) + "\n"
        self._proc.stdin.write(data.encode("utf-8"))
        await self._proc.stdin.drain()

    async def receive(
        self,
    ) -> JSONRPCResponse | JSONRPCError | JSONRPCNotification | None:
        if self._proc is None or self._proc.stdout is None:
            return None

        while True:
            # Check if we already have a complete line buffered.
            if "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.strip()
                if line:
                    return parse_message(line)
                continue

            # Read more data.
            try:
                chunk = await self._proc.stdout.read(4096)
            except (OSError, ValueError):
                return None

            if not chunk:  # EOF
                self._proc = None
                return None

            self._buffer += chunk.decode("utf-8", errors="replace")

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._proc is not None:
            proc = self._proc
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            self._proc = None

    @property
    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _read_stderr(self) -> None:
        """Background task: log server stderr output for diagnostics."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info("MCP server stderr: %s", text)
        except (OSError, asyncio.CancelledError):
            pass


# ── HTTP transport ─────────────────────────────────────────────────────


class HttpTransport(McpTransport):
    """Limited HTTP transport — simple JSON-RPC POST, no SSE / streamable HTTP.

    Each ``send()`` issues a synchronous HTTP POST and stores the parsed
    JSON response body in an internal buffer; ``receive()`` drains that
    buffer on the next call.  This is a *request-response* model only.

    .. important::

       This transport does **not** support the full MCP streamable HTTP
       transport (SSE events, session IDs, server→client notifications,
       resumability, or progress tokens).  It is suitable for basic MCP
       servers that serve a single JSON-RPC response per POST.

       For production use with remote MCP servers that require SSE or
       bidirectional streaming, use ``StdioTransport`` with a local proxy
       or upgrade this class to the full streamable HTTP spec.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._client: httpx.AsyncClient | None = None
        self._pending_response: JSONRPCResponse | JSONRPCError | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **self._headers,
            },
            timeout=httpx.Timeout(30.0),
        )
        # Verify connectivity with a lightweight HEAD/OPTIONS check.
        try:
            resp = await self._client.options(self._url)
            resp.raise_for_status()
        except Exception:
            # Non-fatal: some servers may not support OPTIONS.
            # The real test is the initialize request.
            pass

    async def send(
        self,
        message: JSONRPCRequest | JSONRPCNotification,
    ) -> None:
        if self._client is None:
            raise RuntimeError("HttpTransport not connected")

        data = serialize_message(message)
        resp = await self._client.post(self._url, content=data)
        resp.raise_for_status()
        body = resp.text
        if body:
            parsed = parse_message(body)
            if isinstance(parsed, (JSONRPCResponse, JSONRPCError)):
                self._pending_response = parsed
                return
        self._pending_response = None

    async def receive(
        self,
    ) -> JSONRPCResponse | JSONRPCError | JSONRPCNotification | None:
        # HTTP transport couples send+receive: the response is available
        # immediately after send(). Drain the pending response.
        resp = self._pending_response
        self._pending_response = None
        return resp

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None


__all__ = ["McpTransport", "StdioTransport", "HttpTransport"]
