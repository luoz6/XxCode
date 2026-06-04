"""Unit tests for JSON-RPC 2.0 protocol parsing and serialization."""

import json

from xxcode.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    make_notification,
    make_request,
    parse_message,
    serialize_message,
)


# ── Parse: valid messages ──────────────────────────────────────────────

def test_parse_valid_request():
    data = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {"cursor": ""}})
    msg = parse_message(data)
    assert isinstance(msg, JSONRPCRequest)
    assert msg.id == 42
    assert msg.method == "tools/list"
    assert msg.params == {"cursor": ""}


def test_parse_valid_response():
    data = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"tools": []}})
    msg = parse_message(data)
    assert isinstance(msg, JSONRPCResponse)
    assert msg.id == 7
    assert msg.result == {"tools": []}


def test_parse_error_response():
    data = json.dumps({"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found"}})
    msg = parse_message(data)
    assert isinstance(msg, JSONRPCError)
    assert msg.id == 3
    assert msg.error["code"] == -32601
    assert msg.error["message"] == "Method not found"


def test_parse_notification():
    data = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    msg = parse_message(data)
    assert isinstance(msg, JSONRPCNotification)
    assert msg.method == "notifications/initialized"
    assert msg.params is None


def test_parse_notification_with_params():
    data = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"token": "x"}})
    msg = parse_message(data)
    assert isinstance(msg, JSONRPCNotification)
    assert msg.method == "notifications/progress"
    assert msg.params == {"token": "x"}


# ── Parse: invalid / edge cases ────────────────────────────────────────

def test_parse_malformed_json_returns_none():
    assert parse_message("not json") is None
    assert parse_message("{") is None
    assert parse_message("") is None


def test_parse_missing_jsonrpc_version_returns_none():
    assert parse_message(json.dumps({"id": 1, "method": "ping"})) is None


def test_parse_wrong_jsonrpc_version_returns_none():
    assert parse_message(json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"})) is None


def test_parse_non_dict_returns_none():
    assert parse_message(json.dumps([1, 2, 3])) is None
    assert parse_message(json.dumps("string")) is None


# ── Serialize ──────────────────────────────────────────────────────────

def test_serialize_request_round_trip():
    req = make_request("tools/call", {"name": "search", "arguments": {"q": "test"}}, 99)
    raw = serialize_message(req)
    parsed = parse_message(raw)
    assert isinstance(parsed, JSONRPCRequest)
    assert parsed.method == "tools/call"
    assert parsed.id == 99
    assert parsed.params == {"name": "search", "arguments": {"q": "test"}}


def test_serialize_notification():
    notif = make_notification("notifications/initialized")
    raw = serialize_message(notif)
    parsed = parse_message(raw)
    assert isinstance(parsed, JSONRPCNotification)
    assert parsed.method == "notifications/initialized"


def test_serialize_response():
    resp = JSONRPCResponse(id=5, result={"status": "ok"})
    raw = serialize_message(resp)
    parsed = parse_message(raw)
    assert isinstance(parsed, JSONRPCResponse)
    assert parsed.id == 5
    assert parsed.result == {"status": "ok"}


# ── Helpers ────────────────────────────────────────────────────────────

def test_make_request_creates_valid_request():
    req = make_request("ping", None, 1)
    assert isinstance(req, JSONRPCRequest)
    assert req.method == "ping"
    assert req.id == 1
    assert req.params is None


def test_make_notification_creates_valid_notification():
    n = make_notification("notifications/initialized")
    assert isinstance(n, JSONRPCNotification)
    assert n.method == "notifications/initialized"
    assert "id" not in n.__dict__ or n.__dict__.get("id") is None


# ── Standard error codes ───────────────────────────────────────────────

def test_standard_error_codes_defined():
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601
    assert INVALID_PARAMS == -32602
    assert INTERNAL_ERROR == -32603
