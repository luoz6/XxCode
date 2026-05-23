"""Configuration management using environment variables and pydantic Settings."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Agent configuration loaded from environment variables and defaults."""

    # API settings
    api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    api_base_url: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    api_model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
    api_max_tokens: int = 16000

    # YOLO mode — skip all permission prompts
    yolo: bool = field(default_factory=lambda: os.environ.get("YOLO_MODE", "").lower() in ("1", "true", "yes"))

    # Limits
    max_tool_output_chars: int = 50_000
    shell_timeout_seconds: float = 30.0
    shell_max_output_bytes: int = 5 * 1024 * 1024  # 5MB

    # Compression
    context_compress_threshold: float = 0.85  # trigger compression at 85% context window

    # Session
    session_dir: Path = field(default_factory=lambda: Path.home() / ".xxcode" / "sessions")

    # Working directory
    cwd: Path = field(default_factory=Path.cwd)


_config: Config | None = None


def get_config() -> Config:
    """Get the global config singleton."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def set_config(config: Config) -> None:
    """Set the global config singleton."""
    global _config
    _config = config
