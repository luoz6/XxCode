"""Unit tests for the local skill system."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.document import Document

from xxcode.cli.completer import XxCodeCompleter
from xxcode.cli.commands import iter_command_help_rows
from xxcode.config import Config
from xxcode.skills.discovery import SkillDiscovery
from xxcode.skills.executor import (
    SKILL_INLINE_SOURCE,
    SkillExecutionResult,
    SkillExecutor,
)
from xxcode.skills.loader import SkillLoader
from xxcode.skills.models import (
    SkillFrontmatter,
    SkillParseError,
    SkillSource,
    SkillSpec,
    parse_skill_md,
    validate_frontmatter,
)
from xxcode.skills.persistence import InvokedSkillRecord, SkillPersistence
from xxcode.skills.prompt_processor import PromptProcessor, SkillShellPermissionRequest
from xxcode.skills.registry import SkillRegistry
from xxcode.skills.security import decide_inline_shell_execution
from xxcode.skills.runtime import (
    SKILL_TRANSIENT_SOURCES,
    InlineSkillRuntime,
    collect_inline_skill_runtime,
    strip_skill_context_messages,
)
from xxcode.skills.tool import SkillTool, SkillToolInput
from xxcode.tools.registry import ToolRegistry


class _FakeAsyncProcess:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout.encode("utf-8")
        self.stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _write_skill_md(
    dir_path: Path,
    name: str,
    frontmatter: str,
    body: str = "Skill body content.",
) -> Path:
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    content = f"---\n{frontmatter}\n---\n\n{body}\n"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def _make_skill(
    name: str,
    *,
    source: SkillSource = SkillSource.PROJECT,
    content: str | None = None,
    paths: list[str] | None = None,
    user_invocable: bool = True,
    disable_model_invocation: bool = False,
    directory: Path | None = None,
) -> SkillSpec:
    frontmatter = SkillFrontmatter(
        name=name,
        description=f"{name} description",
        paths=paths,
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
    )
    return SkillSpec(
        frontmatter=frontmatter,
        source=source,
        directory=directory,
        skill_file=(directory / "SKILL.md") if directory is not None else None,
        canonical_name=name,
        content=content,
    )


class TestParseSkillMd:
    def test_valid_skill(self, tmp_path):
        skill_file = _write_skill_md(
            tmp_path,
            "review",
            "name: review\ndescription: Review code changes.\n",
        )
        frontmatter, content = parse_skill_md(skill_file)
        assert frontmatter.name == "review"
        assert frontmatter.description == "Review code changes."
        assert frontmatter.context == "inline"
        assert frontmatter.user_invocable is True
        assert "Skill body content" in content

    def test_missing_frontmatter(self, tmp_path):
        skill_file = _write_skill_md(tmp_path, "bad", "name: bad")
        skill_file.write_text("Just plain text, no YAML.", encoding="utf-8")
        with pytest.raises(SkillParseError, match="Missing YAML frontmatter"):
            parse_skill_md(skill_file)

    def test_missing_description_uses_dir_name(self, tmp_path):
        skill_file = _write_skill_md(tmp_path, "myskill", "", body="Body.")
        skill_file.write_text("---\nname: myskill\n---\n\nBody.\n", encoding="utf-8")
        frontmatter, content = parse_skill_md(skill_file)
        assert frontmatter.description == "myskill"
        assert content == "Body.\n"

    def test_kebab_case_conversion(self, tmp_path):
        skill_file = _write_skill_md(
            tmp_path,
            "my-skill",
            (
                "name: my-skill\n"
                "description: Does things.\n"
                "when-to-use: when needed\n"
                "user-invocable: false\n"
            ),
        )
        frontmatter, _ = parse_skill_md(skill_file)
        assert frontmatter.when_to_use == "when needed"
        assert frontmatter.user_invocable is False

    def test_invalid_context_defaults_to_inline(self, tmp_path):
        skill_file = _write_skill_md(
            tmp_path,
            "bad",
            "name: bad\ndescription: Test.\ncontext: unknown\n",
        )
        frontmatter, _ = parse_skill_md(skill_file)
        assert frontmatter.context == "inline"


class TestSkillLoader:
    def test_loads_bundled_skills(self, tmp_path):
        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / "user-skills")

        loader = SkillLoader(config)
        skills = loader.load_frontmatter_only()
        bundled = {
            skill.canonical_name: skill
            for skill in skills
            if skill.source == SkillSource.BUNDLED
        }

        assert "review" in bundled
        assert "commit" in bundled

    def test_load_frontmatter_only(self, tmp_path):
        _write_skill_md(
            tmp_path / ".xxcode" / "skills",
            "review",
            "name: review\ndescription: Review code.\n",
        )
        _write_skill_md(
            tmp_path / ".xxcode" / "skills",
            "commit",
            "name: commit\ndescription: Create commits.\n",
        )

        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / ".xxcode" / "user-skills")

        loader = SkillLoader(config)
        skills = loader.load_frontmatter_only()
        names = {skill.canonical_name for skill in skills}
        assert {"review", "commit"} <= names

    def test_project_skill_wins_over_bundled_same_name(self, tmp_path):
        _write_skill_md(
            tmp_path / ".xxcode" / "skills",
            "review",
            "name: review\ndescription: Project review.\n",
        )

        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / "user-skills")

        loader = SkillLoader(config)
        registry = SkillRegistry()
        for skill in loader.load_frontmatter_only():
            registry.register(skill)

        chosen = registry.find("review")
        assert chosen is not None
        assert chosen.source == SkillSource.PROJECT

    def test_user_skill_wins_same_name(self, tmp_path):
        _write_skill_md(
            tmp_path / ".xxcode" / "skills",
            "review",
            "name: review\ndescription: Project review.\n",
        )
        _write_skill_md(
            tmp_path / "user-skills",
            "review",
            "name: review\ndescription: User review.\n",
        )

        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / "user-skills")

        loader = SkillLoader(config)
        registry = SkillRegistry()
        for skill in loader.load_frontmatter_only():
            registry.register(skill)

        chosen = registry.find("review")
        assert chosen is not None
        assert chosen.source == SkillSource.USER

    def test_lazy_loading(self, tmp_path):
        _write_skill_md(
            tmp_path / ".xxcode" / "skills",
            "review",
            "name: review\ndescription: Review code.\n",
            "Full body.",
        )

        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / ".xxcode" / "nonexistent")

        loader = SkillLoader(config)
        skill = next(
            entry
            for entry in loader.load_frontmatter_only()
            if entry.canonical_name == "review" and entry.source == SkillSource.PROJECT
        )
        assert skill.content is None

        loaded = loader.load_full_content(skill)
        assert loaded.content is not None
        assert "Full body" in loaded.content

    def test_skips_invalid_skill(self, tmp_path):
        skill_dir = tmp_path / ".xxcode" / "skills" / "broken"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("Not a valid skill file.", encoding="utf-8")

        config = Config(cwd=tmp_path)
        config.skills_dir = ".xxcode/skills"
        config.user_skills_dir = str(tmp_path / ".xxcode" / "nonexistent")

        loader = SkillLoader(config)
        skills = loader.load_frontmatter_only()
        names = {skill.canonical_name for skill in skills}
        assert "broken" not in names


class TestSkillRegistry:
    def test_find_first_registered_wins(self):
        registry = SkillRegistry()
        first = _make_skill("test")
        registry.register(first)
        registry.register(_make_skill("test"))
        assert registry.find("test") is first

    def test_list_user_invocable(self):
        registry = SkillRegistry()
        registry.register(_make_skill("visible", user_invocable=True))
        registry.register(_make_skill("hidden", user_invocable=False))
        invocable = registry.list_user_invocable()
        names = {skill.canonical_name for skill in invocable}
        assert "visible" in names
        assert "hidden" not in names

    def test_list_model_invocable(self):
        registry = SkillRegistry()
        registry.register(_make_skill("auto"))
        registry.register(_make_skill("manual", disable_model_invocation=True))
        invocable = registry.list_model_invocable()
        names = {skill.canonical_name for skill in invocable}
        assert "auto" in names
        assert "manual" not in names

    def test_paths_filtering_and_find_visible(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("global"))
        registry.register(
            _make_skill("react-only", paths=["src/components/**"])
        )

        assert registry.find_visible("global", tmp_path) is not None
        assert registry.find_visible("react-only", tmp_path) is None
        visible = registry.list_visible(tmp_path / "src" / "components")
        names = {skill.canonical_name for skill in visible}
        assert {"global", "react-only"} <= names

    def test_paths_filtering_requires_path_boundary_match(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("components-only", paths=["src/components/**"]))

        (tmp_path / "src" / "components").mkdir(parents=True)
        (tmp_path / "packages" / "src" / "components" / "button").mkdir(parents=True)
        (tmp_path / "src" / "components-legacy").mkdir(parents=True)
        (tmp_path / "src" / "component").mkdir(parents=True)

        assert registry.find_visible("components-only", tmp_path / "src" / "components") is not None
        assert registry.find_visible("components-only", tmp_path / "packages" / "src" / "components" / "button") is None
        assert registry.find_visible("components-only", tmp_path / "src" / "components-legacy") is None
        assert registry.find_visible("components-only", tmp_path / "src" / "component") is None

    def test_paths_filtering_supports_exact_relative_file_like_pattern(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("ui-root", paths=["src/ui"]))

        (tmp_path / "src" / "ui").mkdir(parents=True)
        (tmp_path / "packages" / "web" / "src" / "ui").mkdir(parents=True)
        (tmp_path / "src" / "ui-kit").mkdir(parents=True)

        assert registry.find_visible("ui-root", tmp_path / "src" / "ui") is not None
        assert registry.find_visible("ui-root", tmp_path / "packages" / "web" / "src" / "ui") is None
        assert registry.find_visible("ui-root", tmp_path / "src" / "ui-kit") is None

    def test_paths_filtering_requires_double_star_for_nested_relative_match(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("nested-ui", paths=["**/src/ui"]))

        (tmp_path / "packages" / "web" / "src" / "ui").mkdir(parents=True)

        assert registry.find_visible("nested-ui", tmp_path / "packages" / "web" / "src" / "ui") is not None

    def test_paths_filtering_supports_negation(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(
            _make_skill(
                "frontend-only",
                paths=["src/**", "!src/legacy/**"],
            )
        )

        assert registry.find_visible("frontend-only", tmp_path / "src" / "app") is not None
        assert registry.find_visible("frontend-only", tmp_path / "src" / "legacy" / "old-ui") is None

    def test_paths_filtering_supports_wildcards_and_directory_patterns(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("docs", paths=["docs/", "src/*/ui"]))

        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / "docs-file").write_text("echo", encoding="utf-8")
        (tmp_path / "src" / "web" / "ui").mkdir(parents=True)
        (tmp_path / "src" / "web" / "admin" / "ui").mkdir(parents=True)

        assert registry.find_visible("docs", tmp_path / "docs") is not None
        assert registry.find_visible("docs", tmp_path / "docs-file") is None
        assert registry.find_visible("docs", tmp_path / "src" / "web" / "ui") is not None
        assert registry.find_visible("docs", tmp_path / "src" / "web" / "admin" / "ui") is None

    def test_paths_filtering_directory_pattern_matches_directory_not_same_name_file(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("ui-dir", paths=["src/ui/"]))

        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "ui").write_text("script", encoding="utf-8")
        (tmp_path / "src" / "ui-dir").mkdir()

        assert registry.find_visible("ui-dir", tmp_path / "src" / "ui") is None
        assert registry.find_visible("ui-dir", tmp_path / "src" / "ui-dir") is None

        (tmp_path / "src" / "ui").unlink()
        (tmp_path / "src" / "ui").mkdir()

        assert registry.find_visible("ui-dir", tmp_path / "src" / "ui") is not None

    def test_paths_filtering_supports_single_char_wildcard(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("pkg", paths=["src/pkg-?/ui"]))

        (tmp_path / "src" / "pkg-a" / "ui").mkdir(parents=True)
        (tmp_path / "src" / "pkg-ab" / "ui").mkdir(parents=True)

        assert registry.find_visible("pkg", tmp_path / "src" / "pkg-a" / "ui") is not None
        assert registry.find_visible("pkg", tmp_path / "src" / "pkg-ab" / "ui") is None

    def test_paths_filtering_supports_root_anchored_patterns(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(_make_skill("root-ui", paths=["/src/ui/**"]))

        (tmp_path / "src" / "ui").mkdir(parents=True)
        (tmp_path / "packages" / "src" / "ui").mkdir(parents=True)

        assert registry.find_visible("root-ui", tmp_path / "src" / "ui") is not None
        assert registry.find_visible("root-ui", tmp_path / "packages" / "src" / "ui") is None

    def test_paths_invalid_scalar_fails_closed(self, tmp_path):
        frontmatter = validate_frontmatter(
            {"name": "review", "description": "x", "paths": "src/**"},
            file_path=tmp_path / "review" / "SKILL.md",
        )
        skill = SkillSpec(
            frontmatter=frontmatter,
            source=SkillSource.PROJECT,
            directory=tmp_path / "review",
            skill_file=None,
            canonical_name="review",
            content="body",
        )
        registry = SkillRegistry(root=tmp_path)
        registry.register(skill)

        assert registry.find_visible("review", tmp_path) is None

    def test_paths_invalid_list_item_falls_back_to_global_visibility(self, tmp_path):
        frontmatter = validate_frontmatter(
            {"name": "review", "description": "x", "paths": [123]},
            file_path=tmp_path / "review" / "SKILL.md",
        )
        skill = SkillSpec(
            frontmatter=frontmatter,
            source=SkillSource.PROJECT,
            directory=tmp_path / "review",
            skill_file=None,
            canonical_name="review",
            content="body",
        )
        registry = SkillRegistry(root=tmp_path)
        registry.register(skill)

        assert frontmatter.paths is None
        assert registry.find_visible("review", tmp_path) is not None


class TestPromptProcessor:
    def test_substitutes_arguments_and_env_vars(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content=(
                "file=$file mode=${mode} all=$ARGUMENTS idx=$0/$1 list=$ARGUMENTS[0] "
                "dir=${XXCODE_SKILL_DIR} session=${CLAUDE_SESSION_ID}"
            ),
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )
        skill.frontmatter.arguments = ["file", "mode"]

        rendered = asyncio.run(
            processor.process(
                skill,
                "app.py strict",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "file=app.py" in rendered
        assert "mode=strict" in rendered
        assert "all=app.py strict" in rendered
        assert "idx=app.py/strict" in rendered
        assert "list=app.py" in rendered
        assert "session=sess-123" in rendered
        assert str(skill.directory).replace("\\", "/") in rendered

    def test_argument_index_placeholder_is_not_corrupted_by_arguments_placeholder(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="all=$ARGUMENTS first=$ARGUMENTS[0]",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )

        rendered = asyncio.run(
            processor.process(
                skill,
                "app.py strict",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "all=app.py strict" in rendered
        assert "first=app.py" in rendered
        assert "[0]" not in rendered

    def test_appends_arguments_when_no_placeholders(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="Review the repository.",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )

        rendered = asyncio.run(
            processor.process(
                skill,
                "foo bar",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert rendered.rstrip().endswith("ARGUMENTS: foo bar")

    def test_preserves_quoted_arguments_for_index_and_named_placeholders(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="file=$file mode=$1 all=$ARGUMENTS first=$0",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )
        skill.frontmatter.arguments = ["file", "mode"]

        rendered = asyncio.run(
            processor.process(
                skill,
                '"src/my file.py" strict',
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "file=src/my file.py" in rendered
        assert "mode=strict" in rendered
        assert "all=\"src/my file.py\" strict" in rendered
        assert "first=src/my file.py" in rendered

    def test_named_argument_replacement_does_not_overlap_longer_names(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="plain=$file overlap=$file_mode brace=${file_mode}",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )
        skill.frontmatter.arguments = ["file", "file_mode"]

        rendered = asyncio.run(
            processor.process(
                skill,
                "app.py strict",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "plain=app.py" in rendered
        assert "overlap=strict" in rendered
        assert "brace=strict" in rendered
        assert "app.py_mode" not in rendered

    def test_positional_args_descending_order_prevents_partial_match(self, tmp_path):
        """$10 must be matched before $1 to avoid $1 capturing the leading digit."""
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "test",
            source=SkillSource.USER,
            content="$1 $10",
            directory=tmp_path / ".xxcode" / "skills" / "test",
        )
        # 2 args only — without descending sort $1 would match inside $10
        rendered = asyncio.run(
            processor.process(
                skill,
                "first second",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "$1" not in rendered
        assert "$10" not in rendered
        # descending: $10→"" (no arg), $1→first → "...first "
        assert rendered.rstrip().endswith("second")

    def test_positional_args_clears_missing_placeholders(self, tmp_path):
        """$N with no corresponding arg must be replaced with empty string."""
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "test",
            source=SkillSource.USER,
            content="[$0] [$1] [$2]",
            directory=tmp_path / ".xxcode" / "skills" / "test",
        )
        rendered = asyncio.run(
            processor.process(
                skill,
                "only-one",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "$2" not in rendered
        assert rendered.rstrip().endswith("[only-one] [] []")

    def test_user_skill_shell_executes(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        requests: list[SkillShellPermissionRequest] = []

        async def approve(request: SkillShellPermissionRequest) -> bool:
            requests.append(request)
            return True

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=approve,
            )
        )
        assert requests and requests[0].command == "echo hello"
        assert "Command says: hello" in rendered

    def test_bundled_skill_shell_executes(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.BUNDLED,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "Command says: hello" in rendered

    def test_project_skill_shell_requests_approval(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.PROJECT,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        requests: list[SkillShellPermissionRequest] = []

        async def approve(request: SkillShellPermissionRequest) -> bool:
            requests.append(request)
            return True

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=approve,
            )
        )
        assert requests and requests[0].command == "echo hello"
        assert "Command says: hello" in rendered

    def test_inline_shell_replacement_uses_original_match_boundaries(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.PROJECT,
            content="A !`cmd1` B !`cmd2`",
            directory=tmp_path,
        )

        async def approve(_request: SkillShellPermissionRequest) -> bool:
            return True

        async def fake_run(command: str, *, cwd, executable=None) -> str:
            if command == "cmd1":
                return "!`cmd2`"
            if command == "cmd2":
                return "DONE"
            raise AssertionError(f"Unexpected command: {command}")

        processor._run_inline_shell = fake_run  # type: ignore[method-assign]

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=approve,
            )
        )

        assert "A !`cmd2` B DONE" in rendered
        assert rendered.count("DONE") == 1

    def test_user_skill_shell_now_requests_approval(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        requests: list[SkillShellPermissionRequest] = []

        async def approve(request: SkillShellPermissionRequest) -> bool:
            requests.append(request)
            return True

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=approve,
            )
        )
        assert requests and requests[0].command == "echo hello"
        assert "Command says: hello" in rendered

    def test_bundled_skill_custom_shell_requests_approval(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.BUNDLED,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        skill.frontmatter.shell = "bash"
        requests: list[SkillShellPermissionRequest] = []

        async def approve(request: SkillShellPermissionRequest) -> bool:
            requests.append(request)
            return True

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeAsyncProcess(stdout="hello")),
        ) as mock_exec:
            rendered = asyncio.run(
                processor.process(
                    skill,
                    "",
                    session_id="sess-123",
                    approve_project_shell=approve,
                )
            )
        assert requests and requests[0].command == "echo hello"
        assert mock_exec.call_args.args[:3] == ("bash", "-c", "echo hello")
        assert "Command says: hello" in rendered

    def test_untrusted_skill_source_shell_is_blocked(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        skill.source = "mcp"  # type: ignore[assignment]

        with pytest.raises(PermissionError, match="not allowed"):
            asyncio.run(
                processor.process(
                    skill,
                    "",
                    session_id="sess-123",
                    approve_project_shell=lambda request: True,
                )
            )

    def test_custom_shell_executable_is_rejected_when_not_allowlisted(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        skill.frontmatter.shell = "python"

        with pytest.raises(PermissionError, match="not allowed"):
            asyncio.run(
                processor.process(
                    skill,
                    "",
                    session_id="sess-123",
                    approve_project_shell=lambda request: True,
                )
            )


class TestPromptProcessorAdditions:
    def test_argument_hint_on_empty_args(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="Review the repository.",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )
        skill.frontmatter.argument_hint = "<file> <mode>"

        rendered = asyncio.run(
            processor.process(
                skill,
                "",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "Hint: <file> <mode>" in rendered

    def test_argument_hint_skipped_when_args_provided(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="Review the repository.",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )
        skill.frontmatter.argument_hint = "<file>"

        rendered = asyncio.run(
            processor.process(
                skill,
                "foo.py",
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )
        assert "Hint:" not in rendered
        assert "ARGUMENTS: foo.py" in rendered

    def test_malformed_quoted_arguments_fall_back_to_raw_string(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "review",
            source=SkillSource.USER,
            content="all=$ARGUMENTS first=$0",
            directory=tmp_path / ".xxcode" / "skills" / "review",
        )

        rendered = asyncio.run(
            processor.process(
                skill,
                '"unterminated',
                session_id="sess-123",
                approve_project_shell=lambda request: True,
            )
        )

        assert 'all="unterminated' in rendered
        assert 'first="unterminated' in rendered

    def test_shell_field_as_executable(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello`",
            directory=tmp_path,
        )
        skill.frontmatter.shell = "bash"

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeAsyncProcess(stdout="hello")),
        ) as mock_exec:
            rendered = asyncio.run(
                processor.process(
                    skill,
                    "",
                    session_id="sess-123",
                    approve_project_shell=lambda request: True,
                )
            )
            assert mock_exec.call_args.args[:3] == ("bash", "-c", "echo hello")
            assert mock_exec.call_args.kwargs["cwd"] == str(tmp_path)
            assert "Command says: hello" in rendered

    def test_inline_shell_commands_execute_in_source_order(self, tmp_path):
        config = Config(cwd=tmp_path)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="first=!`echo one` second=!`echo two`",
            directory=tmp_path,
        )

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(
                side_effect=[
                    _FakeAsyncProcess(stdout="one"),
                    _FakeAsyncProcess(stdout="two"),
                ]
            ),
        ) as mock_exec:
            rendered = asyncio.run(
                processor.process(
                    skill,
                    "",
                    session_id="sess-123",
                    approve_project_shell=lambda request: True,
                )
            )

        assert "first=one second=two" in rendered
        assert [exec_call.args[-1] for exec_call in mock_exec.call_args_list] == [
            "echo one",
            "echo two",
        ]
        assert all(
            exec_call.kwargs["cwd"] == str(tmp_path)
            for exec_call in mock_exec.call_args_list
        )

    def test_inline_shell_output_respects_max_bytes(self, tmp_path):
        config = Config(cwd=tmp_path, shell_max_output_bytes=8)
        processor = PromptProcessor(config)
        skill = _make_skill(
            "echo",
            source=SkillSource.USER,
            content="Command says: !`echo hello world`",
            directory=tmp_path,
        )

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeAsyncProcess(stdout="hello world")),
        ):
            with pytest.raises(RuntimeError, match="output exceeded"):
                asyncio.run(
                    processor.process(
                        skill,
                        "",
                        session_id="sess-123",
                        approve_project_shell=lambda request: True,
                    )
                )


class TestSkillSecurity:
    def test_decide_inline_shell_execution_for_sources(self):
        user_skill = _make_skill("user-shell", source=SkillSource.USER, content="!`echo hi`")
        project_skill = _make_skill("project-shell", source=SkillSource.PROJECT, content="!`echo hi`")
        bundled_skill = _make_skill("bundled-shell", source=SkillSource.BUNDLED, content="!`echo hi`")
        bundled_custom = _make_skill("bundled-bash", source=SkillSource.BUNDLED, content="!`echo hi`")
        bundled_custom.frontmatter.shell = "bash"

        user_decision = decide_inline_shell_execution(user_skill, "echo hi")
        project_decision = decide_inline_shell_execution(project_skill, "echo hi")
        bundled_decision = decide_inline_shell_execution(bundled_skill, "echo hi")
        bundled_custom_decision = decide_inline_shell_execution(bundled_custom, "echo hi")

        assert user_decision.allowed and user_decision.requires_approval
        assert project_decision.allowed and project_decision.requires_approval
        assert bundled_decision.allowed and not bundled_decision.requires_approval
        assert bundled_custom_decision.allowed and bundled_custom_decision.requires_approval


class TestSkillDiscovery:
    def test_budget_prefers_user_skill_descriptions(self):
        registry = SkillRegistry()
        user_skill = _make_skill("review", source=SkillSource.USER)
        user_skill.frontmatter.description = "Review the current code changes carefully."
        project_skill = _make_skill("commit", source=SkillSource.PROJECT)
        project_skill.frontmatter.description = "Create a conventional commit message for the staged diff."
        registry.register(user_skill)
        registry.register(project_skill)

        discovery = SkillDiscovery(registry)
        text = discovery.format_listing(registry.list_model_invocable(), budget_chars=170)

        assert "<system-reminder>" in text
        assert "- review:" in text
        assert "- commit" in text

    def test_budget_preserves_bundled_descriptions(self):
        registry = SkillRegistry()
        bundled_skill = _make_skill("review", source=SkillSource.BUNDLED)
        bundled_skill.frontmatter.description = "Bundled review description."
        user_skill = _make_skill("alpha", source=SkillSource.USER)
        user_skill.frontmatter.description = (
            "Alpha user skill description that should be truncated to a name."
        )
        project_skill = _make_skill("beta", source=SkillSource.PROJECT)
        project_skill.frontmatter.description = (
            "Beta project skill description that should be truncated to a name."
        )
        registry.register(user_skill)
        registry.register(project_skill)
        registry.register(bundled_skill)

        discovery = SkillDiscovery(registry)
        header = (
            "<system-reminder>\n"
            "The following skills are available for use with the Skill tool:\n\n"
        )
        footer = "\n</system-reminder>"
        bundled_entry = discovery._format_full_entry(bundled_skill)
        names_only_body = "\n".join([bundled_entry, "- alpha", "- beta"])
        budget_chars = len(header) + len(footer) + len(names_only_body) + 16

        text = discovery.format_listing(
            registry.list_model_invocable(),
            budget_chars=budget_chars,
            preserve_bundled=True,
        )

        assert "- review:" in text
        assert "- alpha" in text
        assert "- beta" in text
        assert "- alpha:" not in text
        assert "- beta:" not in text

    def test_clip_respects_small_limits(self):
        assert SkillDiscovery._clip("abcdef", 0) == ""
        assert SkillDiscovery._clip("abcdef", 3) == "..."
        assert len(SkillDiscovery._clip("abcdef", 2)) == 2


class TestSkillPersistence:
    def test_recovery_attachment_prefers_recent_records(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))

        persistence.record_invocation("main", "older", "/older", "older prompt", turn_count=1)
        persistence.record_invocation("main", "newer", "/newer", "newer prompt", turn_count=5)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert attachment.index("newer") < attachment.index("older")

    def test_clear_scope_removes_records(self):
        persistence = SkillPersistence()
        persistence.record_invocation("main", "review", "/review", "prompt", turn_count=1)
        assert persistence.build_recovery_attachment("main") is not None
        persistence.clear_for_scope("main")
        assert persistence.build_recovery_attachment("main") is None

    def test_recovery_attachment_skips_oversized_recent_record(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([30.0, 20.0, 10.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_SKILLS_TOKEN_BUDGET",
            57,
        )
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_MAX_TOKENS_PER_SKILL",
            40,
        )

        persistence.record_invocation("main", "recent-large", "/recent-large", "X" * 100, turn_count=9)
        persistence.record_invocation("main", "middle-small", "/middle-small", "small-middle", turn_count=5)
        persistence.record_invocation("main", "older-small", "/older-small", "small-older", turn_count=1)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert "recent-large" not in attachment
        assert "middle-small" in attachment
        assert "older-small" in attachment

    def test_recovery_prefers_compact_records_when_budget_is_tight(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([30.0, 20.0, 10.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_SKILLS_TOKEN_BUDGET",
            45,
        )
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_MAX_TOKENS_PER_SKILL",
            20,
        )

        persistence.record_invocation("main", "recent-huge", "/recent-huge", "X" * 80, turn_count=9)
        persistence.record_invocation("main", "m", "/m", "mid", turn_count=5)
        persistence.record_invocation("main", "o", "/o", "old", turn_count=1)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert "recent-huge" not in attachment
        assert "## Skill: m" in attachment
        assert "## Skill: o" in attachment

    def test_recovery_keeps_only_latest_record_per_skill_name(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0, 30.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))

        persistence.record_invocation("main", "review", "/review/old", "old prompt", turn_count=1)
        persistence.record_invocation("main", "review", "/review/new", "new prompt", turn_count=3)
        persistence.record_invocation("main", "commit", "/commit", "commit prompt", turn_count=2)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert attachment.count("## Skill: review") == 1
        assert "/review/new" in attachment
        assert "/review/old" not in attachment

    def test_recovery_prefers_more_recent_value_when_section_sizes_match(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0, 30.0, 40.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))

        persistence.record_invocation("main", "alpha", "/same", "compact", turn_count=7)
        persistence.record_invocation("main", "alpha", "/same", "compact", turn_count=8)
        persistence.record_invocation("main", "bravo", "/same", "compact", turn_count=9)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert attachment.index("bravo") < attachment.index("alpha")

    def test_recovery_prefers_compact_section_when_value_is_equal(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))

        persistence.record_invocation("main", "tiny", "/x", "compact", turn_count=5)
        persistence.record_invocation(
            "main",
            "much-longer-name",
            "/much/longer/path",
            "compact",
            turn_count=5,
        )

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert attachment.index("tiny") < attachment.index("much-longer-name")

    def test_recovery_prefers_more_recent_turn_when_frequency_matches(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))

        persistence.record_invocation("main", "older", "/older", "compact", turn_count=2)
        persistence.record_invocation("main", "newer", "/newer", "compact", turn_count=6)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert attachment.index("newer") < attachment.index("older")

    def test_recovery_section_budget_counts_header_and_truncates_content_once(self):
        persistence = SkillPersistence()
        record = InvokedSkillRecord(
            name="skill",
            path="/path",
            content="X" * 40,
            invoked_at=1.0,
            agent_scope="main",
        )

        candidates = persistence._build_candidates([record], max_chars_per_skill=32)
        _record, section = candidates[0]
        header = "## Skill: skill\nPath: /path\n\n"
        max_content_chars = 32 - len(header)
        expected_content = "..."
        if max_content_chars > 3:
            expected_content = ("X" * (max_content_chars - 3)) + "..."
        expected_section = header + expected_content

        assert len(section) == 32
        assert section == expected_section

    def test_recovery_selection_handles_empty_prompt_without_division_error(self, monkeypatch):
        persistence = SkillPersistence()
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: 10.0)

        persistence.record_invocation("main", "blank", "/blank", " ", turn_count=1)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert "blank" in attachment

    def test_import_snapshot_skips_malformed_record_values(self):
        persistence = SkillPersistence()
        snapshot = {
            "version": 1,
            "agent_scopes": {
                "main": {
                    "good": {
                        "name": "good",
                        "path": "/good",
                        "content": "good prompt",
                        "invoked_at": 1.5,
                        "agent_scope": "main",
                        "last_turn_index": 3,
                        "invocation_count": 2,
                    },
                    "bad": {
                        "name": "bad",
                        "path": "/bad",
                        "content": "bad prompt",
                        "invoked_at": "abc",
                        "agent_scope": "main",
                        "last_turn_index": "oops",
                        "invocation_count": "oops",
                    },
                }
            },
        }

        persistence.import_snapshot(snapshot)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert "good" in attachment
        assert "bad" not in attachment

    def test_import_snapshot_invalid_version_preserves_existing_records(self, monkeypatch):
        persistence = SkillPersistence()
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: 10.0)
        persistence.record_invocation("main", "review", "/review", "prompt", turn_count=1)

        persistence.import_snapshot({"version": 999})

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert "review" in attachment

    def test_recovery_attachment_respects_total_message_budget(self, monkeypatch):
        persistence = SkillPersistence()
        timestamps = iter([10.0, 20.0])
        monkeypatch.setattr("xxcode.skills.persistence.time.time", lambda: next(timestamps))
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_SKILLS_TOKEN_BUDGET",
            35,
        )
        monkeypatch.setattr(
            "xxcode.skills.persistence.POST_COMPACT_MAX_TOKENS_PER_SKILL",
            20,
        )

        persistence.record_invocation("main", "a", "/a", "x", turn_count=2)
        persistence.record_invocation("main", "b", "/b", "y", turn_count=1)

        attachment = persistence.build_recovery_attachment("main")
        assert attachment is not None
        assert len(attachment) <= 35 * 4
        assert attachment.count("## Skill:") == 1


class TestForkExecutor:
    def test_apply_fork_effort_none(self):
        prompt = "Do something."
        result = SkillExecutor._apply_fork_effort(prompt, None)
        assert result == prompt

    def test_apply_fork_effort_quick(self):
        result = SkillExecutor._apply_fork_effort("Do something.", "quick")
        assert "quick" in result
        assert "shortest viable path" in result

    def test_apply_fork_effort_standard(self):
        result = SkillExecutor._apply_fork_effort("Do something.", "standard")
        assert "standard" in result
        assert "reliable completion" in result

    def test_apply_fork_effort_custom(self):
        result = SkillExecutor._apply_fork_effort("Do something.", 5)
        assert "effort: 5" in result

    def test_resolve_fork_thinking_budget(self):
        assert SkillExecutor._resolve_fork_thinking_budget(None) is None
        assert SkillExecutor._resolve_fork_thinking_budget("quick") == 1024
        assert SkillExecutor._resolve_fork_thinking_budget("standard") == 4096
        assert SkillExecutor._resolve_fork_thinking_budget(0) is None
        assert SkillExecutor._resolve_fork_thinking_budget(1) == 1
        assert SkillExecutor._resolve_fork_thinking_budget(-5) is None

    def test_build_fork_registry_allowed_tools(self, tmp_path):
        from xxcode.tools.base import Tool

        class _FakeTool(Tool):
            name = "stub"
            description = "stub"
            input_schema = None  # type: ignore[assignment]

            async def execute(self, input, context):
                return "ok"

            def is_read_only(self, input=None):
                return True

        config = Config(cwd=tmp_path)
        loader = SkillLoader(config)
        prompt_processor = PromptProcessor(config)
        executor = SkillExecutor(loader, prompt_processor)

        base = ToolRegistry()
        tool_a = _FakeTool()
        tool_a.name = "a"
        tool_b = _FakeTool()
        tool_b.name = "b"
        base.register(tool_a)
        base.register(tool_b)

        filtered = executor._build_fork_registry(base, allowed_tools=["a"])
        filtered_tools = filtered.list_tools()
        filtered_names = {t.name for t in filtered_tools}
        assert "a" in filtered_names
        assert "b" not in filtered_names

    def test_build_fork_registry_none_base(self):
        config = Config()
        loader = SkillLoader(config)
        prompt_processor = PromptProcessor(config)
        executor = SkillExecutor(loader, prompt_processor)

        result = executor._build_fork_registry(None, allowed_tools=None)
        assert len(result.list_tools()) == 0

    def test_build_fork_registry_empty_allow_list_disables_all_tools(self, tmp_path):
        from xxcode.tools.base import Tool

        class _FakeTool(Tool):
            name = "stub"
            description = "stub"
            input_schema = None  # type: ignore[assignment]

            async def execute(self, input, context):
                return "ok"

            def is_read_only(self, input=None):
                return True

        config = Config(cwd=tmp_path)
        loader = SkillLoader(config)
        prompt_processor = PromptProcessor(config)
        executor = SkillExecutor(loader, prompt_processor)

        base = ToolRegistry()
        tool_a = _FakeTool()
        tool_a.name = "a"
        base.register(tool_a)

        filtered = executor._build_fork_registry(base, allowed_tools=[])
        assert filtered.list_tools() == []

    def test_invalid_allowed_tools_falls_back_to_read_only_pool(self, tmp_path):
        from xxcode.tools.file_read import ReadFileTool
        from xxcode.tools.file_write import WriteFileTool

        frontmatter = validate_frontmatter(
            {
                "name": "audit",
                "description": "x",
                "context": "fork",
                "allowed-tools": [123],
            },
            file_path=tmp_path / "audit" / "SKILL.md",
        )
        assert frontmatter.allowed_tools is None

        config = Config(cwd=tmp_path)
        loader = SkillLoader(config)
        prompt_processor = PromptProcessor(config)
        executor = SkillExecutor(loader, prompt_processor)

        base = ToolRegistry([ReadFileTool(), WriteFileTool()])
        filtered = executor._build_fork_registry(base, allowed_tools=frontmatter.allowed_tools)
        filtered_names = sorted(tool.name for tool in filtered.list_tools())

        assert "read_file" in filtered_names
        assert "write_file" not in filtered_names


class TestSkillTool:
    @staticmethod
    def _make_tool(registry, executor=None):
        if executor is None:
            executor = MagicMock()
        return SkillTool(registry, executor)

    def test_validate_input_rejects_empty_skill(self):
        tool = self._make_tool(SkillRegistry())
        ok, msg = asyncio.run(
            tool.validate_input(SkillToolInput(skill=""), {})
        )
        assert not ok
        assert "must not be empty" in msg

    def test_validate_input_rejects_unknown_skill(self, tmp_path):
        tool = self._make_tool(SkillRegistry())
        ok, msg = asyncio.run(
            tool.validate_input(
                SkillToolInput(skill="nonexistent"),
                {"cwd": str(tmp_path)},
            )
        )
        assert not ok
        assert "not found" in msg

    def test_validate_input_rejects_disable_model_invocation(self, tmp_path):
        registry = SkillRegistry()
        registry.register(_make_skill("manual", disable_model_invocation=True))
        tool = self._make_tool(registry)
        ok, msg = asyncio.run(
            tool.validate_input(
                SkillToolInput(skill="manual"),
                {"cwd": str(tmp_path)},
            )
        )
        assert not ok
        assert "cannot be invoked automatically" in msg

    def test_validate_input_accepts_valid_skill(self, tmp_path):
        registry = SkillRegistry()
        registry.register(_make_skill("review"))
        tool = self._make_tool(registry)
        ok, msg = asyncio.run(
            tool.validate_input(
                SkillToolInput(skill="review"),
                {"cwd": str(tmp_path)},
            )
        )
        assert ok
        assert msg == ""

    def test_execute_inline_injects_pending_messages(self, tmp_path):
        registry = SkillRegistry()
        skill = _make_skill(
            "review",
            source=SkillSource.PROJECT,
            content="Review the changes.",
            directory=tmp_path,
        )
        registry.register(skill)

        exec_result = SkillExecutionResult(
            mode="inline",
            prompt="Rendered prompt content.",
        )

        mock_executor = MagicMock()
        async def _mock_execute(*args, **kwargs):
            return exec_result
        mock_executor.execute = _mock_execute
        mock_executor.build_inline_skill_message = MagicMock(
            return_value={"role": "user", "content": [{"type": "text", "text": "msg"}]}
        )

        tool = self._make_tool(registry, executor=mock_executor)
        context: dict = {"cwd": str(tmp_path), "_registry": MagicMock()}

        result = asyncio.run(
            tool.execute(SkillToolInput(skill="review", args=""), context)
        )
        assert "injected" in result
        pending = context.get("_pending_skill_messages", [])
        assert len(pending) == 1

    def test_execute_fork_returns_result_text(self, tmp_path):
        registry = SkillRegistry()
        skill = _make_skill(
            "task",
            source=SkillSource.PROJECT,
            content="Perform a task.",
            directory=tmp_path,
        )
        skill.frontmatter.context = "fork"
        registry.register(skill)

        exec_result = SkillExecutionResult(
            mode="fork",
            prompt="Fork prompt.",
            result_text="Fork completed.",
        )

        mock_executor = MagicMock()
        async def _mock_fork_execute(*args, **kwargs):
            return exec_result
        mock_executor.execute = _mock_fork_execute

        tool = self._make_tool(registry, executor=mock_executor)
        context: dict = {"cwd": str(tmp_path), "_registry": MagicMock()}

        result = asyncio.run(
            tool.execute(SkillToolInput(skill="task", args=""), context)
        )
        assert "Fork completed" in result
        assert "_pending_skill_messages" not in context


class TestRuntime:
    def test_strip_removes_transient_sources(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "inline"}],
                "metadata": {"source": SKILL_INLINE_SOURCE},
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "listing"}],
                "metadata": {"source": "skill_listing"},
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "recovery"}],
                "metadata": {"source": "skill_recovery"},
            },
        ]
        stripped = strip_skill_context_messages(messages)
        assert len(stripped) == 1
        assert stripped[0]["content"] == "Hello"

    def test_strip_preserves_non_meta_messages(self):
        messages = [
            {"role": "user", "content": "Keep me"},
            {"role": "user", "content": "Me too"},
        ]
        stripped = strip_skill_context_messages(messages)
        assert len(stripped) == 2

    def test_strip_with_custom_sources(self):
        messages = [
            {"role": "user", "content": "A"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "B"}],
                "metadata": {"source": "custom"},
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "C"}],
                "metadata": {"source": SKILL_INLINE_SOURCE},
            },
        ]
        stripped = strip_skill_context_messages(messages, sources=["custom"])
        assert len(stripped) == 2
        sources_left = {m.get("metadata", {}).get("source") for m in stripped}
        assert "custom" not in sources_left
        assert SKILL_INLINE_SOURCE in sources_left

    def test_strip_with_empty_sources_preserves_skill_meta_messages(self):
        messages = [
            {"role": "user", "content": "A"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "B"}],
                "metadata": {"source": SKILL_INLINE_SOURCE},
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "C"}],
                "metadata": {"source": "skill_listing"},
            },
        ]

        stripped = strip_skill_context_messages(messages, sources=[])

        assert stripped == messages

    def test_transient_sources_contains_expected(self):
        assert SKILL_INLINE_SOURCE in SKILL_TRANSIENT_SOURCES
        assert "skill_listing" in SKILL_TRANSIENT_SOURCES
        assert "skill_recovery" in SKILL_TRANSIENT_SOURCES
        assert len(SKILL_TRANSIENT_SOURCES) == 3

    def test_collect_inline_skill_runtime_no_skills(self):
        messages: list = []
        runtime = collect_inline_skill_runtime(messages)
        assert runtime.allowed_tool_names is None

    def test_collect_inline_skill_runtime_with_allowed_tools(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "msg"}],
                "metadata": {
                    "source": SKILL_INLINE_SOURCE,
                    "xxcode_skill_allowed_tools": ["read_file", "grep_search"],
                },
            },
        ]
        runtime = collect_inline_skill_runtime(messages)
        assert runtime.allowed_tool_names is not None
        assert "read_file" in runtime.allowed_tool_names
        assert "grep_search" in runtime.allowed_tool_names

    def test_collect_inline_skill_runtime_intersects_multiple(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "msg1"}],
                "metadata": {
                    "source": SKILL_INLINE_SOURCE,
                    "xxcode_skill_allowed_tools": ["a", "b", "c"],
                },
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "msg2"}],
                "metadata": {
                    "source": SKILL_INLINE_SOURCE,
                    "xxcode_skill_allowed_tools": ["b", "c", "d"],
                },
            },
        ]
        runtime = collect_inline_skill_runtime(messages)
        assert runtime.allowed_tool_names == frozenset({"b", "c"})


class TestCompleter:
    def test_skill_completion_is_not_duplicated(self, tmp_path):
        registry = SkillRegistry()
        registry.register(_make_skill("review"))
        completer = XxCodeCompleter(skill_registry=registry, cwd=tmp_path)
        completions = list(
            completer.get_completions(Document("/re"), complete_event=None)
        )
        texts = [completion.text for completion in completions]
        assert texts.count("/review") == 1

    def test_path_completion_tracks_updated_cwd(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        (first / "alpha.txt").write_text("a", encoding="utf-8")
        (second / "beta.txt").write_text("b", encoding="utf-8")

        completer = XxCodeCompleter(cwd=first)
        first_completions = list(
            completer.get_completions(Document(""), complete_event=None)
        )
        assert any(item.display_text == "alpha.txt" for item in first_completions)
        assert all(item.display_text != "beta.txt" for item in first_completions)

        completer._cwd = second
        second_completions = list(
            completer.get_completions(Document(""), complete_event=None)
        )
        assert any(item.display_text == "beta.txt" for item in second_completions)
        assert all(item.display_text != "alpha.txt" for item in second_completions)

    def test_path_completion_keeps_dotfiles_visible_except_dot_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".env").write_text("x", encoding="utf-8")
        (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
        (tmp_path / ".claude").mkdir()

        completer = XxCodeCompleter(cwd=tmp_path)
        completions = list(
            completer.get_completions(Document("."), complete_event=None)
        )
        displays = {item.display_text for item in completions}

        assert ".env" in displays
        assert ".gitignore" in displays
        assert ".claude/" in displays
        assert ".git/" not in displays

    def test_help_rows_share_builtin_and_skill_metadata(self, tmp_path):
        registry = SkillRegistry(root=tmp_path)
        registry.register(
            _make_skill(
                "review",
                directory=tmp_path / ".xxcode" / "skills" / "review",
            )
        )

        rows = iter_command_help_rows(skill_registry=registry, cwd=tmp_path)

        assert ("/save, /s", "Save session to disk") in rows
        assert (
            "/cost, /tokens",
            "Show token usage and API cost breakdown",
        ) in rows
        assert (
            "/compact, /compress",
            "Manually compress conversation context",
        ) in rows
        assert ("/skill", "Show visible skills for current directory") in rows
        assert ("/mcp", "Show registered MCP tools for current session") in rows
        assert all(command != "/sessions" for command, _ in rows)
        assert ("/quit, /q, /exit", "Exit XxCode") in rows
        assert ("/review", "review description") in rows
