"""MCP (Model Context Protocol) integration for XxCode.

Provides JSON-RPC 2.0 protocol types, stdio/HTTP transports, client lifecycle
management with tool discovery, and MCP server configuration loading.

Key exports:
  - Protocol: JSONRPCRequest, JSONRPCResponse, JSONRPCError, JSONRPCNotification
  - Transport: McpTransport (ABC), StdioTransport, HttpTransport
  - Client: McpClient, ConnectionState, McpError
  - Config: McpServerConfig, load_mcp_config
"""

from .client import ConnectionState, McpClient, McpError
from .config import McpServerConfig, load_mcp_config
from .content import extract_content
from .protocol import (
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)
from .transport import HttpTransport, McpTransport, StdioTransport

__all__ = [
    # Protocol
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "JSONRPCNotification",
    # Transport
    "McpTransport",
    "StdioTransport",
    "HttpTransport",
    # Client
    "McpClient",
    "ConnectionState",
    "McpError",
    # Config
    "McpServerConfig",
    "load_mcp_config",
    # Content
    "extract_content",
]
