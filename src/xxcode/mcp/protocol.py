"""JSON-RPC 2.0 protocol types and serialization for MCP.

The Model Context Protocol (MCP) is built on JSON-RPC 2.0. This module provides
the message envelope types and parse/serialize helpers needed to communicate
with MCP servers.

Implements the subset of JSON-RPC 2.0 required by MCP:
  - Request:    {jsonrpc, id, method, params?}
  - Response:   {jsonrpc, id, result}
  - Error:      {jsonrpc, id, error: {code, message, data?}}
  - Notification: {jsonrpc, method, params?}  (no id)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── Standard JSON-RPC 2.0 error codes ──────────────────────────────────

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific error codes (defined by MCP spec, not JSON-RPC)
MCP_SERVER_NOT_INITIALIZED = -32002
MCP_UNKNOWN_ERROR = -32001


# ── Message types ──────────────────────────────────────────────────────


@dataclass
class JSONRPCRequest:
    """Outgoing request with unique id."""
    jsonrpc: str = "2.0"
    id: int = 0
    method: str = ""
    params: dict[str, Any] | None = None


@dataclass
class JSONRPCResponse:
    """Successful response matching a request id."""
    jsonrpc: str = "2.0"
    id: int = 0
    result: Any = None


@dataclass
class JSONRPCError:
    """Error response matching a request id."""
    jsonrpc: str = "2.0"
    id: int = 0
    error: dict[str, Any] = field(default_factory=dict)


@dataclass
class JSONRPCNotification:
    """One-way notification (no id field, no response expected)."""
    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] | None = None


# ── Serialization helpers ──────────────────────────────────────────────


def serialize_message(
    msg: JSONRPCRequest | JSONRPCResponse | JSONRPCError | JSONRPCNotification,
) -> str:
    """Serialize a JSON-RPC message to a compact single-line JSON string."""
    data: dict[str, Any] = {"jsonrpc": msg.jsonrpc}
    if hasattr(msg, "id"):
        data["id"] = msg.id  # type: ignore[union-attr]
    if hasattr(msg, "method"):
        data["method"] = msg.method  # type: ignore[union-attr]
    if isinstance(msg, JSONRPCRequest) and msg.params is not None:
        data["params"] = msg.params
    if isinstance(msg, JSONRPCNotification) and msg.params is not None:
        data["params"] = msg.params
    if isinstance(msg, JSONRPCResponse):
        data["result"] = msg.result
    if isinstance(msg, JSONRPCError):
        data["error"] = msg.error
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_message(
    data: str,
) -> JSONRPCRequest | JSONRPCResponse | JSONRPCError | JSONRPCNotification | None:
    """Parse a JSON string into the appropriate JSON-RPC message type.

    Dispatch logic:
      - Has "method" but no "id" → Notification
      - Has "method" and "id"   → Request
      - Has "id" and "result"   → Response
      - Has "id" and "error"    → Error
      - Otherwise               → None

    Returns None for unparseable JSON or unrecognized message shapes.
    """
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
        return None

    has_id = "id" in obj
    has_method = "method" in obj
    has_result = "result" in obj
    has_error = "error" in obj

    if has_method and not has_id:
        return JSONRPCNotification(
            method=str(obj["method"]),
            params=obj.get("params"),
        )
    if has_method and has_id:
        return JSONRPCRequest(
            id=int(obj["id"]),
            method=str(obj["method"]),
            params=obj.get("params"),
        )
    if has_id and has_error:
        err = obj["error"]
        if isinstance(err, dict):
            return JSONRPCError(
                id=int(obj["id"]),
                error={
                    "code": err.get("code", 0),
                    "message": str(err.get("message", "")),
                    "data": err.get("data"),
                },
            )
        return None
    if has_id and has_result:
        return JSONRPCResponse(
            id=int(obj["id"]),
            result=obj["result"],
        )
    return None


def make_request(method: str, params: dict[str, Any] | None, request_id: int) -> JSONRPCRequest:
    """Create a new JSON-RPC request with the given id."""
    return JSONRPCRequest(method=method, params=params, id=request_id)


def make_notification(method: str, params: dict[str, Any] | None = None) -> JSONRPCNotification:
    """Create a new JSON-RPC notification (no response expected)."""
    return JSONRPCNotification(method=method, params=params)


__all__ = [
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "JSONRPCNotification",
    "parse_message",
    "serialize_message",
    "make_request",
    "make_notification",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "MCP_SERVER_NOT_INITIALIZED",
    "MCP_UNKNOWN_ERROR",
]
