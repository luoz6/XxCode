"""Security helpers for skill trust and inline shell execution."""

from __future__ import annotations

from dataclasses import dataclass

from .models import SkillSource, SkillSpec

SAFE_INLINE_SHELL_EXECUTABLES = frozenset({
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
})


@dataclass(frozen=True, slots=True)
class SkillShellDecision:
    """Decision describing whether an inline shell command may run."""

    allowed: bool
    requires_approval: bool = False
    reason: str | None = None
    normalized_executable: str | None = None


def decide_inline_shell_execution(skill: SkillSpec, command: str) -> SkillShellDecision:
    """Apply source trust and shell executable policy to one inline command."""
    stripped_command = command.strip()
    if not stripped_command:
        return SkillShellDecision(
            allowed=False,
            reason=f"Skill '{skill.canonical_name}' has an empty inline shell command.",
        )

    executable = _normalize_shell_executable(skill.frontmatter.shell)
    if skill.frontmatter.shell and executable not in SAFE_INLINE_SHELL_EXECUTABLES:
        return SkillShellDecision(
            allowed=False,
            reason=(
                f"Skill '{skill.canonical_name}' shell executable "
                f"'{skill.frontmatter.shell}' is not allowed for inline shell commands."
            ),
            normalized_executable=executable,
        )

    if skill.source == SkillSource.BUNDLED:
        return SkillShellDecision(
            allowed=True,
            requires_approval=bool(skill.frontmatter.shell),
            reason=(
                "Bundled skill requests a custom shell executable."
                if skill.frontmatter.shell
                else None
            ),
            normalized_executable=executable,
        )

    if skill.source in {SkillSource.USER, SkillSource.PROJECT}:
        return SkillShellDecision(
            allowed=True,
            requires_approval=True,
            reason=f"{skill.source.value.title()} skills require approval for inline shell commands.",
            normalized_executable=executable,
        )

    return SkillShellDecision(
        allowed=False,
        reason=f"Skill source '{skill.source}' is not allowed to execute inline shell commands.",
        normalized_executable=executable,
    )


def _normalize_shell_executable(executable: str | None) -> str | None:
    if not executable:
        return None
    normalized = executable.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return normalized or None


__all__ = [
    "SAFE_INLINE_SHELL_EXECUTABLES",
    "SkillShellDecision",
    "decide_inline_shell_execution",
]
