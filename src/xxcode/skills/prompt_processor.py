"""Skill prompt rendering pipeline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .models import SkillSpec
from .security import decide_inline_shell_execution
from .shell_safety import run_shell_safety_checks

_INLINE_SHELL_RE = re.compile(r"!\`(?P<command>.+?)\`", re.DOTALL)
_ARGUMENTS_INDEX_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]")
_ARGUMENTS_ALL_RE = re.compile(r"\$ARGUMENTS(?!\[)")

logger = logging.getLogger(__name__)

# Sources allowed to execute inline shell commands (design §6.3).
# Remote / untrusted sources such as MCP are explicitly excluded.


@dataclass(slots=True)
class SkillShellPermissionRequest:
    """One project-skill inline shell command waiting for approval."""

    skill_name: str
    command: str


class PromptProcessor:
    """Render a skill prompt from markdown + runtime arguments."""

    def __init__(self, config: Config):
        self._config = config

    @property
    def config(self) -> Config:
        return self._config

    async def process(
        self,
        skill: SkillSpec,
        args: str,
        *,
        session_id: str,
        approve_project_shell,
    ) -> str:
        if skill.content is None:
            raise ValueError("Skill content must be loaded before processing")

        content = skill.content

        if skill.directory is not None:
            content = f"Base directory for this skill: {skill.directory}\n\n{content}"

        content = self._substitute_arguments(
            content, args, skill.frontmatter.arguments,
            argument_hint=skill.frontmatter.argument_hint,
        )
        content = self._substitute_env_vars(content, skill.directory, session_id, skill.source)
        content = await self._execute_inline_shell(
            content,
            skill,
            approve_project_shell=approve_project_shell,
        )
        return content

    def _substitute_arguments(
        self,
        content: str,
        args: str,
        named_args: list[str] | None,
        *,
        argument_hint: str | None = None,
    ) -> str:
        raw_args = args.strip()
        if raw_args:
            try:
                argv = shlex.split(raw_args)
            except ValueError:
                logger.warning(
                    "Skill argument parsing failed due to malformed quoting; "
                    "falling back to the raw argument string."
                )
                argv = [raw_args]
        else:
            argv = []
        replacements_applied = False

        def _replace_arguments_index(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            return argv[idx] if idx < len(argv) else ""

        content, indexed_count = _ARGUMENTS_INDEX_RE.subn(
            _replace_arguments_index,
            content,
        )
        replacements_applied |= indexed_count > 0

        content, all_count = _ARGUMENTS_ALL_RE.subn(raw_args, content)
        replacements_applied |= all_count > 0

        # Replace positional args in descending-index order so that $10 is
        # matched before $1, preventing $1 from incorrectly capturing the
        # leading digit of a multi-digit placeholder.
        positional_refs = re.findall(r"\$(\d+)", content)
        sorted_indices = sorted({int(i) for i in positional_refs}, reverse=True)
        for idx in sorted_indices:
            marker = f"${idx}"
            value = argv[idx] if idx < len(argv) else ""
            content = content.replace(marker, value)
            replacements_applied = True

        if named_args:
            value_map = {
                name: (argv[idx] if idx < len(argv) else "")
                for idx, name in enumerate(named_args)
            }
            escaped_names = sorted(
                (re.escape(name) for name in named_args),
                key=len,
                reverse=True,
            )
            if escaped_names:
                braced_pattern = re.compile(
                    r"\$\{(" + "|".join(escaped_names) + r")\}"
                )
                plain_pattern = re.compile(
                    r"\$(" + "|".join(escaped_names) + r")(?![A-Za-z0-9_])"
                )

                content, braced_count = braced_pattern.subn(
                    lambda match: value_map[match.group(1)],
                    content,
                )
                replacements_applied |= braced_count > 0

                content, plain_count = plain_pattern.subn(
                    lambda match: value_map[match.group(1)],
                    content,
                )
                replacements_applied |= plain_count > 0

        if not replacements_applied and raw_args:
            content = content.rstrip() + f"\n\nARGUMENTS: {raw_args}\n"
        elif not replacements_applied and not raw_args and argument_hint:
            content = content.rstrip() + f"\n\nHint: {argument_hint}\n"

        return content

    def _substitute_env_vars(
        self,
        content: str,
        skill_dir: Path | None,
        session_id: str,
        source: str,
    ) -> str:
        # Skill-identifying env vars are not substituted for MCP / other
        # remote sources to prevent local path disclosure (design §6.3).
        if source not in {"mcp"} and skill_dir is not None:
            skill_dir_str = str(skill_dir).replace("\\", "/")
            content = content.replace("${XXCODE_SKILL_DIR}", skill_dir_str)
            content = content.replace("${CLAUDE_SKILL_DIR}", skill_dir_str)

        content = content.replace("${XXCODE_SESSION_ID}", session_id)
        content = content.replace("${CLAUDE_SESSION_ID}", session_id)
        return content

    async def _execute_inline_shell(
        self,
        content: str,
        skill: SkillSpec,
        *,
        approve_project_shell,
    ) -> str:
        matches = list(_INLINE_SHELL_RE.finditer(content))
        if not matches:
            return content

        commands: list[str] = []
        executable = skill.frontmatter.shell or None
        for match in matches:
            command = match.group("command").strip()
            decision = decide_inline_shell_execution(skill, command)
            if not decision.allowed:
                raise PermissionError(
                    decision.reason or "Inline shell command is not allowed."
                )

            if decision.requires_approval:
                approval_result = approve_project_shell(
                    SkillShellPermissionRequest(
                        skill_name=skill.canonical_name,
                        command=command,
                    )
                )
                approved = (
                    await approval_result
                    if inspect.isawaitable(approval_result)
                    else approval_result
                )
                if not approved:
                    raise PermissionError(
                        f"Skill '{skill.canonical_name}' shell command was denied: {command}"
                    )
            commands.append(command)

        replacements: list[str] = []
        for command in commands:
            replacements.append(
                await self._run_inline_shell(
                    command,
                    cwd=skill.directory,
                    executable=executable,
                )
            )

        rendered_parts: list[str] = []
        cursor = 0
        for match, replacement in zip(matches, replacements, strict=True):
            rendered_parts.append(content[cursor:match.start()])
            rendered_parts.append(replacement)
            cursor = match.end()
        rendered_parts.append(content[cursor:])
        return "".join(rendered_parts)

    async def _run_inline_shell(
        self, command: str, *, cwd: Path | None, executable: str | None = None
    ) -> str:
        run_cwd = cwd if cwd is not None else self._config.cwd
        run_shell_safety_checks(command, run_cwd)

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *_shell_argv(command, executable),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(run_cwd),
            )
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.shell_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            if proc is not None:
                proc.kill()
                await proc.wait()
            raise RuntimeError(
                f"Inline shell command timed out after "
                f"{self._config.shell_timeout_seconds}s: {command}"
            ) from exc

        stdout = stdout_raw.decode("utf-8", errors="replace").strip()
        stderr = stderr_raw.decode("utf-8", errors="replace").strip()
        if proc.returncode:
            raise RuntimeError(
                f"Inline shell command failed ({proc.returncode}): {command}\n{stderr}"
            )
        if len(stdout.encode("utf-8", errors="replace")) > self._config.shell_max_output_bytes:
            raise RuntimeError(
                f"Inline shell command output exceeded {self._config.shell_max_output_bytes} bytes: {command}"
            )
        return stdout


def _shell_argv(command: str, executable: str | None) -> list[str]:
    """Build an explicit shell argv for inline skill commands."""
    if executable:
        name = Path(executable).name.lower()
        if name in {"cmd", "cmd.exe"}:
            return [executable, "/C", command]
        if name.startswith("powershell") or name == "pwsh":
            return [executable, "-NoProfile", "-Command", command]
        return [executable, "-c", command]

    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/C", command]
    return [os.environ.get("SHELL", "/bin/sh"), "-c", command]


__all__ = ["PromptProcessor", "SkillShellPermissionRequest"]
