"""Shared slash-command metadata for help text and completion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    """Metadata for one slash command family."""

    primary: str
    description: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.primary, *self.aliases)

    @property
    def help_label(self) -> str:
        return ", ".join(self.names)


BUILTIN_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("/help", "Show this help"),
    SlashCommandSpec("/clear", "Start a fresh session"),
    SlashCommandSpec("/save", "Save session to disk", aliases=("/s",)),
    SlashCommandSpec("/cost", "Show token usage and API cost breakdown", aliases=("/tokens",)),
    SlashCommandSpec(
        "/compact",
        "Manually compress conversation context",
        aliases=("/compress",),
    ),
    SlashCommandSpec("/yolo", "Toggle YOLO mode (skip all permission prompts)"),
    SlashCommandSpec("/skill", "Show visible skills for current directory"),
    SlashCommandSpec("/mcp", "Show registered MCP tools for current session"),
    SlashCommandSpec("/resume", "Resume a previous session by ID"),
    SlashCommandSpec("/quit", "Exit XxCode", aliases=("/q", "/exit")),
)

BUILTIN_COMMANDS: list[str] = [
    name
    for spec in BUILTIN_COMMAND_SPECS
    for name in spec.names
]

COMMAND_META: dict[str, str] = {}
for _spec in BUILTIN_COMMAND_SPECS:
    COMMAND_META[_spec.primary] = _spec.description
    for _alias in _spec.aliases:
        COMMAND_META[_alias] = f"Alias for {_spec.primary}"


def iter_command_completion_items(
    *,
    skill_registry: Any | None = None,
    cwd: Path | None = None,
) -> list[tuple[str, str]]:
    """Return slash-command completion items for built-ins and visible skills."""
    items: list[tuple[str, str]] = [
        (name, COMMAND_META.get(name, ""))
        for name in BUILTIN_COMMANDS
    ]
    if skill_registry is None or cwd is None:
        return items

    for skill in skill_registry.list_user_invocable(cwd):
        items.append(
            (
                f"/{skill.canonical_name}",
                skill.frontmatter.argument_hint or skill.frontmatter.description,
            )
        )
    return items


def iter_command_help_rows(
    *,
    skill_registry: Any | None = None,
    cwd: Path | None = None,
) -> list[tuple[str, str]]:
    """Return grouped help rows for built-ins and visible skill commands."""
    rows = [(spec.help_label, spec.description) for spec in BUILTIN_COMMAND_SPECS]
    if skill_registry is None or cwd is None:
        return rows

    for skill in skill_registry.list_user_invocable(cwd):
        rows.append(
            (
                f"/{skill.canonical_name}",
                skill.frontmatter.argument_hint or skill.frontmatter.description,
            )
        )
    return rows


__all__ = [
    "BUILTIN_COMMANDS",
    "BUILTIN_COMMAND_SPECS",
    "COMMAND_META",
    "SlashCommandSpec",
    "iter_command_completion_items",
    "iter_command_help_rows",
]
