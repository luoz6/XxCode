"""Register MCP server tools into the XxCode ToolRegistry."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ...mcp.client import McpClient
from ...mcp.config import (
    McpServerConfig,
    load_project_mcp_config,
    load_trusted_mcp,
    load_user_mcp_config,
    save_trusted_mcp,
)
from ..registry import ToolRegistry
from .dynamic_tool import McpTool
from .resource_tools import ListMcpResourcesTool, ReadMcpResourceTool
from .schema import build_mcp_input_model

logger = logging.getLogger(__name__)


def _sanitize_mcp_tool_name(server_name: str, tool_name: str) -> str:
    safe_server = re.sub(r"[^a-z0-9_]", "_", server_name.lower())
    safe_tool = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return f"mcp__{safe_server}__{safe_tool}"


async def register_mcp_tools(
    registry: ToolRegistry,
    cwd: str | Path,
    context: dict[str, Any],
) -> None:
    """Discover configured MCP servers and register their tools.

    This adapter intentionally lives under ``tools.mcp`` because it bridges
    the pure MCP client layer into XxCode's internal ToolRegistry.
    """
    project_root = Path(cwd)
    try:
        configs = _trusted_configs(project_root, context)
    except Exception as exc:
        logger.warning("Failed to load MCP config: %s", exc)
        return

    if not configs:
        logger.debug("No MCP servers configured.")
        return

    clients: dict[str, McpClient] = {}
    context["mcp_clients"] = clients
    registered_count = 0

    for cfg in configs:
        client = McpClient(_config=cfg)
        try:
            connected = await client.connect()
            if not connected:
                logger.warning(
                    "Failed to connect to MCP server '%s': %s",
                    cfg.name,
                    client.last_error or "unknown error",
                )
                continue

            tools = await client.discover_tools()
            logger.info("MCP server '%s': discovered %d tools", cfg.name, len(tools))
            clients[cfg.name] = client

            for tool_def in tools:
                tool_name = tool_def["name"]
                safe_name = _sanitize_mcp_tool_name(cfg.name, tool_name)
                schema = tool_def.get("inputSchema", {})
                tool_desc = tool_def.get("description", f"MCP tool: {tool_name}")
                input_model = build_mcp_input_model(tool_name, schema)

                instance = McpTool.from_definition(
                    public_name=safe_name,
                    server_name=cfg.name,
                    tool_name=tool_name,
                    description=f"[MCP Server: {cfg.name}] {tool_desc}",
                    input_schema=input_model,
                    raw_schema=schema,
                    should_defer=True,
                    search_hint=f"mcp {cfg.name} {tool_name} {tool_desc}",
                )
                registry.register(instance)
                registered_count += 1
        except Exception as exc:
            logger.warning("Error connecting to MCP server '%s': %s", cfg.name, exc)

    if clients:
        registry.register_class(
            ListMcpResourcesTool,
            should_defer=True,
            search_hint="mcp list resources",
        )
        registry.register_class(
            ReadMcpResourceTool,
            should_defer=True,
            search_hint="mcp resource read fetch uri",
        )

    logger.info(
        "MCP: registered %d tools across %d server(s)",
        registered_count,
        len(clients),
    )


def _trusted_configs(project_root: Path, context: dict[str, Any]) -> list[McpServerConfig]:
    configs = list(load_user_mcp_config())
    project_configs = load_project_mcp_config(project_root)
    if not project_configs:
        return configs

    trusted = load_trusted_mcp(project_root)
    trusted_changed = False
    pending: list[dict[str, Any]] = []
    trusted_project: list[McpServerConfig] = []

    for cfg in project_configs:
        fingerprint = cfg.fingerprint()
        if trusted.get(cfg.name) == fingerprint:
            trusted_project.append(cfg)
            continue
        pending.append(_trust_prompt_payload(cfg, fingerprint))

    if pending:
        context.setdefault("pending_mcp_trust", []).extend(pending)
        logger.warning(
            "Skipped %d project MCP server(s) pending trust approval.",
            len(pending),
        )

    approvals = context.get("trusted_mcp_servers")
    if isinstance(approvals, dict):
        for cfg in project_configs:
            fingerprint = cfg.fingerprint()
            if approvals.get(cfg.name) == fingerprint:
                trusted[cfg.name] = fingerprint
                trusted_changed = True
                trusted_project.append(cfg)

    if trusted_changed:
        save_trusted_mcp(project_root, trusted)

    configs.extend(trusted_project)
    return configs


def _trust_prompt_payload(cfg: McpServerConfig, fingerprint: str) -> dict[str, Any]:
    return {
        "name": cfg.name,
        "fingerprint": fingerprint,
        "command": cfg.command,
        "args": list(cfg.args),
        "cwd": cfg.cwd,
        "url": cfg.url,
    }


__all__ = ["register_mcp_tools"]
