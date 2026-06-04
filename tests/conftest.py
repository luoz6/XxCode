"""Shared pytest configuration and fixtures for XxCode tests."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── Config fixtures ──────────────────────────────────────────────────────

def make_test_config(
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    api_key: str = "test-key",
    api_base_url: str = "https://example.test",
    api_model: str = "test-model",
    api_max_tokens: int = 512,
    auto_memory_enabled: bool = False,
    mcp_enabled: bool = False,
    skills_enabled: bool = True,
    **overrides,
):
    """Create a real Config instance for tests that need the full dataclass."""
    from xxcode.config import Config

    return Config(
        api_key=api_key,
        api_base_url=api_base_url,
        api_model=api_model,
        api_max_tokens=api_max_tokens,
        auto_memory_enabled=auto_memory_enabled,
        mcp_enabled=mcp_enabled,
        skills_enabled=skills_enabled,
        cwd=(cwd or tmp_path),
        session_dir=tmp_path / "sessions",
        user_skills_dir=str(tmp_path / "user-skills"),
        skills_dir=".xxcode/skills",
        **overrides,
    )


def make_fake_config(
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    api_model: str = "fake-model",
    api_key: str = "fake-key",
    api_base_url: str = "http://fake",
    api_max_tokens: int = 1000,
    max_tool_output_chars: int = 1000,
    **overrides,
) -> SimpleNamespace:
    """Create a lightweight SimpleNamespace config for tests."""
    return SimpleNamespace(
        cwd=cwd or tmp_path,
        auto_memory_enabled=False,
        api_model=api_model,
        api_key=api_key,
        api_base_url=api_base_url,
        api_max_tokens=api_max_tokens,
        max_tool_output_chars=max_tool_output_chars,
        session_dir=tmp_path / "sessions",
        **overrides,
    )


@pytest.fixture
def test_config(tmp_path):
    """Fixture: real Config instance."""
    return make_test_config(tmp_path)


@pytest.fixture
def fake_config(tmp_path):
    """Fixture: lightweight SimpleNamespace config."""
    return make_fake_config(tmp_path)
