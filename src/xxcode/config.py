"""Configuration management using environment variables and pydantic Settings."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Agent configuration loaded from environment variables and defaults."""

    # API settings: env-first, override via CLI flags (--api-key, --base-url, --model)
    # Supported models: Claude (claude-*), DeepSeek (deepseek-*), OpenAI (gpt-*, o1, o3, o4)
    api_key: str = field(
        default_factory=lambda: (
            os.environ.get("XXCODE_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
    )
    api_base_url: str = field(
        default_factory=lambda: (
            os.environ.get("XXCODE_API_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or ""
        )
    )
    api_model: str = field(
        default_factory=lambda: (
            os.environ.get("XXCODE_API_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "deepseek-v4-pro"
        )
    )
    api_max_tokens: int = 16000

    # YOLO mode — skip all permission prompts
    yolo: bool = field(default_factory=lambda: os.environ.get("YOLO_MODE", "").lower() in ("1", "true", "yes"))

    # Three-tier tool output limits (Claude Code §4.8)
    # Tier 1: default per-tool character limit
    max_tool_output_chars: int = 50_000
    # Tier 2: absolute per-result ceiling (~100K tokens × 4 chars/token)
    max_tool_result_chars_absolute: int = 400_000
    # Tier 3: aggregate limit across all concurrent results in one message
    max_message_tool_results_chars: int = 200_000
    shell_timeout_seconds: float = 30.0
    shell_max_output_bytes: int = 5 * 1024 * 1024  # 5MB

    # MCP (Model Context Protocol) settings
    mcp_enabled: bool = True
    mcp_ping_interval: float = 30.0
    mcp_connect_timeout: float = 10.0
    mcp_request_timeout: float = 30.0

    # Compression
    context_compress_threshold: float = 0.85  # trigger compression at 85% context window
    prompt_cache_ttl_seconds: float = field(
        default_factory=lambda: float(os.environ.get("XXCODE_PROMPT_CACHE_TTL_SECONDS", "300"))
    )
    anthropic_cache_edits_enabled: bool = field(
        default_factory=lambda: os.environ.get("XXCODE_ANTHROPIC_CACHE_EDITS", "").lower()
        in ("1", "true", "yes")
    )
    max_parent_turns: int = 100

    # Session
    session_dir: Path = field(default_factory=lambda: Path.home() / ".xxcode" / "sessions")

    # Budget & pricing (per 1K tokens) — defaults looked up from pricing table.
    # To override, set XXCODE_INPUT_PRICE_PER_1K / XXCODE_OUTPUT_PRICE_PER_1K.
    max_budget_usd: float = 10.0
    api_input_price_per_1k: float | None = None   # None = auto-lookup from model
    api_output_price_per_1k: float | None = None  # None = auto-lookup from model

    # Auto-memory (cross-session persistent memory)
    auto_memory_enabled: bool = True
    auto_memory_directory: str | None = None  # None = use default path resolution
    extraction_model: str | None = None  # None = inherit api_model for extraction sub-agent
    memory_recall_prefetch_timeout_seconds: float = 0.25

    # Skills
    skills_enabled: bool = True
    skills_dir: str = ".xxcode/skills"
    user_skills_dir: str = str(Path.home() / ".XxCode" / "skills")

    # Terminal UI backend
    ui_backend: str = field(
        default_factory=lambda: os.environ.get(
            "XXCODE_UI_BACKEND",
            "legacy_terminal",
        )
    )

    # Worktree isolation
    worktree_base_ref: str = "HEAD"
    worktree_dir: str = ".xxcode/worktrees"

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
