import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xxcode.agent.subagent import SubAgent
from xxcode.context import builder
from xxcode.context.builder import build_system_prompt
from xxcode.tools.registry import ToolRegistry


SNAPSHOT_DIR = Path(__file__).resolve().parent / "prompt_snapshots"
FAKE_CWD = Path("/fake/project")


class _FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 6, 3)


def _fixed_environment_info() -> dict[str, str]:
    return {
        "cwd": str(FAKE_CWD),
        "platform": "Linux-5.0-test",
        "shell": "/bin/bash",
        "python_version": "3.11.0",
    }


def snapshots_should_update() -> bool:
    return os.environ.get("UPDATE_SNAPSHOTS", "") == "1"


def assert_prompt_snapshot(filename: str, text: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOT_DIR / filename

    if snapshots_should_update():
        target.write_text(text, encoding="utf-8")
        return

    expected = target.read_text(encoding="utf-8")
    assert text == expected


def render_main_prompt_snapshot(
    tmp_path: Path,
    *,
    git_context: str = "",
    project_instructions: str = "",
    memory_section: str = "",
) -> str:
    with (
        patch.object(builder, "date", _FixedDate),
        patch.object(builder, "get_git_context", lambda cwd, compact=False: git_context),
        patch.object(builder, "load_project_instructions", lambda cwd: project_instructions),
        patch.object(builder, "get_environment_info", _fixed_environment_info),
    ):
        return build_system_prompt(FAKE_CWD, memory_section=memory_section)


def render_subagent_prompt_snapshot(
    tmp_path: Path,
    *,
    git_context: str = "",
    auto_memory_enabled: bool = False,
) -> str:
    import xxcode.agent.subagent as subagent_module

    original_date = builder.date
    original_git = subagent_module.get_git_context
    original_env = builder.get_environment_info
    try:
        # build_environment_section() resolves module globals at call time.
        builder.date = _FixedDate
        subagent_module.get_git_context = lambda cwd, compact=False: git_context
        builder.get_environment_info = _fixed_environment_info
        config = SimpleNamespace(
            cwd=FAKE_CWD,
            auto_memory_enabled=auto_memory_enabled,
            api_model="fake",
            api_key="fake",
            api_base_url="http://fake",
            api_max_tokens=1000,
            max_tool_output_chars=1000,
            session_dir=tmp_path / "sessions",
        )
        definition = SimpleNamespace(
            name="test-subagent",
            description="Snapshot test agent.",
            model=None,
            max_turns=5,
        )
        sub = SubAgent(config=config, registry=ToolRegistry(), definition=definition)
        import asyncio

        return asyncio.run(sub._build_system_prompt())
    finally:
        builder.date = original_date
        subagent_module.get_git_context = original_git
        builder.get_environment_info = original_env


def test_prompt_snapshot_dir_is_repo_local():
    assert SNAPSHOT_DIR.parts[-2:] == ("tests", "prompt_snapshots")


def test_snapshot_update_flag_defaults_to_compare_mode(monkeypatch):
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)

    assert snapshots_should_update() is False


def test_snapshot_update_flag_enables_rewrite(monkeypatch):
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")

    assert snapshots_should_update() is True


def test_assert_prompt_snapshot_writes_file_when_update_enabled(tmp_path, monkeypatch):
    from tests import test_prompt_snapshots as mod

    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")

    mod.assert_prompt_snapshot("sample.txt", "hello")

    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "hello"


def test_assert_prompt_snapshot_compares_existing_file(tmp_path, monkeypatch):
    from tests import test_prompt_snapshots as mod

    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")

    mod.assert_prompt_snapshot("sample.txt", "hello")


def test_render_main_prompt_snapshot_freezes_dynamic_inputs(tmp_path):
    rendered = render_main_prompt_snapshot(
        tmp_path,
        git_context="Git branch: main",
        project_instructions="project rules",
    )

    assert "2026-06-03" in rendered
    assert "Git branch: main" in rendered
    assert "project rules" in rendered
    assert FAKE_CWD.as_posix() in rendered
    assert "Linux-5.0-test" in rendered


def test_render_subagent_prompt_snapshot_freezes_dynamic_inputs(tmp_path):
    rendered = render_subagent_prompt_snapshot(
        tmp_path,
        git_context="Git branch: main",
    )

    assert "Git branch: main" in rendered
    assert "test-subagent" in rendered
    assert FAKE_CWD.as_posix() in rendered
    assert "Linux-5.0-test" in rendered


def test_main_prompt_minimal_snapshot(tmp_path):
    text = render_main_prompt_snapshot(tmp_path)
    assert_prompt_snapshot("main_minimal.txt", text)


def test_main_prompt_large_project_instructions_snapshot(tmp_path):
    text = render_main_prompt_snapshot(
        tmp_path,
        project_instructions="local\n\n---\n\n" + ("x" * 6000),
    )
    assert_prompt_snapshot("main_large_project_instructions.txt", text)


def test_main_prompt_large_git_snapshot(tmp_path):
    text = render_main_prompt_snapshot(
        tmp_path,
        git_context="Git branch: main\nWorking tree status:\n  M a.py\n" + ("x" * 3000),
    )
    assert_prompt_snapshot("main_large_git.txt", text)


def test_subagent_minimal_snapshot(tmp_path):
    text = render_subagent_prompt_snapshot(tmp_path)
    assert_prompt_snapshot("subagent_minimal.txt", text)


def test_subagent_compact_git_snapshot(tmp_path):
    text = render_subagent_prompt_snapshot(
        tmp_path,
        git_context="Git branch: main\nWorking tree status:\n  M a.py\n" + ("x" * 3000),
    )
    assert_prompt_snapshot("subagent_compact_git.txt", text)
