"""Unit tests for MCP configuration loading."""

import json
import tempfile
from pathlib import Path

from xxcode.mcp.config import McpServerConfig, load_mcp_config


def _write_mcp_config(root: Path, payload: dict, *, local: bool = False) -> None:
    config_path = root / ".mcp.json"
    if local:
        config_path = root / ".xxcode" / "mcp.json"
        config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_single_stdio_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_mcp_config(root, {
            "mcpServers": {
                "my-server": {
                    "command": "node",
                    "args": ["server.js"],
                }
            }
        })

        configs = load_mcp_config(root)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.name == "my-server"
        assert cfg.command == "node"
        assert cfg.args == ["server.js"]
        assert cfg.is_stdio()
        assert not cfg.is_http()


def test_load_single_http_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_mcp_config(root, {
            "mcpServers": {
                "remote": {
                    "url": "https://api.example.com/mcp",
                    "headers": {"Authorization": "Bearer x"},
                }
            }
        })

        configs = load_mcp_config(root)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.name == "remote"
        assert cfg.url == "https://api.example.com/mcp"
        assert cfg.headers == {"Authorization": "Bearer x"}
        assert cfg.is_http()
        assert not cfg.is_stdio()


def test_load_merge_scopes():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_mcp_config(root, {
            "mcpServers": {
                "shared": {"command": "python", "args": ["v1.py"]},
                "project-only": {"url": "https://project.example.com/mcp"},
            }
        })
        _write_mcp_config(root, {
            "mcpServers": {
                "shared": {"command": "python", "args": ["v2.py"]},  # override
                "local-only": {"url": "https://local.example.com/mcp"},
            }
        }, local=True)

        configs = load_mcp_config(root)
        assert len(configs) == 3

        by_name = {c.name: c for c in configs}
        assert by_name["shared"].args == ["v2.py"]  # local overrides project
        assert "project-only" in by_name
        assert "local-only" in by_name


def test_load_missing_files_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        configs = load_mcp_config(root)
        assert configs == []


def test_load_invalid_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".mcp.json").write_text("not json {{{", encoding="utf-8")

        configs = load_mcp_config(root)
        assert configs == []


def test_load_missing_command_and_url_skipped():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_mcp_config(root, {
            "mcpServers": {
                "bad-server": {"env": {"X": "1"}},  # no command or url
                "good-server": {"command": "node"},
            }
        })

        configs = load_mcp_config(root)
        assert len(configs) == 1
        assert configs[0].name == "good-server"


def test_load_env_and_cwd_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_mcp_config(root, {
            "mcpServers": {
                "env-server": {
                    "command": "python",
                    "env": {"DEBUG": "1"},
                    "cwd": "/tmp",
                }
            }
        })

        configs = load_mcp_config(root)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.env == {"DEBUG": "1"}
        assert cfg.cwd == "/tmp"


def test_mcp_server_config_validate():
    cfg = McpServerConfig(name="test")
    warnings = cfg.validate()
    assert len(warnings) == 1
    assert "missing" in warnings[0].lower()

    cfg2 = McpServerConfig(name="test2", command="node", url="http://x")
    warnings2 = cfg2.validate()
    assert len(warnings2) == 1
    assert "both" in warnings2[0].lower()

    cfg3 = McpServerConfig(name="test3", command="node")
    assert cfg3.validate() == []
