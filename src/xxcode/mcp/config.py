"""MCP server configuration loading and merging.

Supports two config file scopes:
  1. project: {project_root}/.mcp.json
  2. local:   {project_root}/.xxcode/mcp.json

Local config overrides project config for the same server name.
Missing files are silently ignored — MCP is optional.

File format:
  {
    "mcpServers": {
      "server-name": {
        "command": "node",          // stdio transport
        "args": ["server.js"],
        "env": {},
        "cwd": "."
      },
      "remote-server": {
        "url": "https://api.example.com/mcp",  // HTTP transport
        "headers": {}
      }
    }
  }
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server.

    Exactly one of `command` (stdio) or `url` (HTTP) must be set.
    """

    name: str
    # Stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # HTTP transport
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def is_stdio(self) -> bool:
        return self.command is not None

    def is_http(self) -> bool:
        return self.url is not None

    def validate(self) -> list[str]:
        """Validate the config. Returns a list of warning messages."""
        warnings: list[str] = []
        if not self.command and not self.url:
            warnings.append(
                f"MCP server '{self.name}': missing 'command' or 'url' — server will be skipped."
            )
        if self.command and self.url:
            warnings.append(
                f"MCP server '{self.name}': both 'command' and 'url' set — using 'command' (stdio)."
            )
        return warnings

    def fingerprint(self) -> str:
        """Stable fingerprint for trust decisions."""
        payload = {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "url": self.url,
            "headers": self.headers,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_mcp_config(project_root: Path) -> list[McpServerConfig]:
    """Load MCP server configurations from config files, merged by scope.

    Priority (lowest first):
      1. project: {project_root}/.mcp.json
      2. local:   {project_root}/.xxcode/mcp.json

    Local config overwrites project config for the same server name.
    Returns a merged list of McpServerConfig.
    """
    merged: dict[str, dict] = {}

    # Scope 1: project config
    _load_file(project_root / ".mcp.json", merged)

    # Scope 2: local config (overrides project)
    _load_file(project_root / ".xxcode" / "mcp.json", merged)

    return _build_configs(merged)


def load_user_mcp_config() -> list[McpServerConfig]:
    """Load trusted user-level MCP config."""
    merged: dict[str, dict] = {}
    _load_file(Path.home() / ".xxcode" / "mcp.json", merged)
    return _build_configs(merged)


def load_project_mcp_config(project_root: Path) -> list[McpServerConfig]:
    """Load project-level MCP config before trust filtering."""
    merged: dict[str, dict] = {}
    _load_file(project_root / ".mcp.json", merged)
    _load_file(project_root / ".xxcode" / "mcp.json", merged)
    return _build_configs(merged)


def trusted_mcp_path(project_root: Path) -> Path:
    return project_root / ".xxcode" / "trusted_mcp.json"


def load_trusted_mcp(project_root: Path) -> dict[str, str]:
    path = trusted_mcp_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): str(fingerprint)
        for name, fingerprint in servers.items()
        if isinstance(name, str) and isinstance(fingerprint, str)
    }


def save_trusted_mcp(project_root: Path, trusted: dict[str, str]) -> None:
    path = trusted_mcp_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "servers": dict(sorted(trusted.items()))}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_configs(merged: dict[str, dict]) -> list[McpServerConfig]:
    configs: list[McpServerConfig] = []
    for name, raw in merged.items():
        cfg = McpServerConfig(
            name=name,
            command=raw.get("command"),
            args=raw.get("args", []),
            env=raw.get("env", {}),
            cwd=raw.get("cwd"),
            url=raw.get("url"),
            headers=raw.get("headers", {}),
        )
        for warning in cfg.validate():
            logger.warning(warning)
        if cfg.command or cfg.url:
            configs.append(cfg)
    return configs


def _load_file(path: Path, merged: dict[str, dict]) -> None:
    """Load a single MCP config file and merge into the `merged` dict.

    Silently ignores missing files. Logs warnings for malformed JSON.
    """
    if not path.is_file():
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning("MCP config file %s is not valid JSON: %s", path, e)
        return
    except (OSError, PermissionError) as e:
        logger.warning("Cannot read MCP config file %s: %s", path, e)
        return

    if not isinstance(data, dict):
        logger.warning("MCP config file %s has unexpected format (expected object)", path)
        return

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        logger.warning("MCP config file %s missing 'mcpServers' key or not an object", path)
        return

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("MCP server '%s' in %s: config must be an object, skipping", name, path)
            continue
        merged[name] = cfg  # Override with higher-priority scope


__all__ = [
    "McpServerConfig",
    "load_mcp_config",
    "load_project_mcp_config",
    "load_trusted_mcp",
    "load_user_mcp_config",
    "save_trusted_mcp",
    "trusted_mcp_path",
]
