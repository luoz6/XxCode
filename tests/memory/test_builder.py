"""Tests for MEMORY.md-backed memory prompt injection."""

from types import SimpleNamespace

from xxcode.context import builder
from xxcode.context.builder import build_memory_section, build_system_prompt


def _make_memory_config(tmp_path, *, enabled=True):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    return memory_dir, SimpleNamespace(
        auto_memory_enabled=enabled,
        auto_memory_directory=str(memory_dir),
    )


def test_build_memory_section_reads_memory_index(tmp_path):
    memory_dir, config = _make_memory_config(tmp_path)
    (memory_dir / "MEMORY.md").write_text(
        "- [User Role](user-role.md) - User is a data scientist",
        encoding="utf-8",
    )

    section = build_memory_section(config)

    assert str(memory_dir) in section
    assert "MEMORY.md` is the entrypoint index" in section
    assert "provided separately as hidden user context" in section
    assert "User is a data scientist" not in section
    assert "YAML frontmatter" in section


def test_build_memory_section_empty_when_disabled(tmp_path):
    _, config = _make_memory_config(tmp_path, enabled=False)

    assert build_memory_section(config) == ""


def test_build_system_prompt_appends_memory_behavior_after_core_sections(tmp_path, monkeypatch):
    memory_dir, config = _make_memory_config(tmp_path)

    monkeypatch.setattr(builder, "get_git_context", lambda cwd, compact=False: "")
    monkeypatch.setattr(builder, "load_claude_md", lambda cwd: "")

    memory_section = build_memory_section(config)
    prompt = build_system_prompt(tmp_path, memory_section=memory_section)

    assert "## 运行时模型感知" in prompt
    assert "## Persistent Memory" in prompt
    assert prompt.index("## Persistent Memory") > prompt.index("## 运行时模型感知")
