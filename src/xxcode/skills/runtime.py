"""Shared runtime helpers for transient skill context messages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import SKILL_LISTING_SOURCE
from .executor import SKILL_INLINE_ALLOWED_TOOLS_KEY, SKILL_INLINE_SOURCE
from .persistence import SKILL_RECOVERY_SOURCE

SKILL_TRANSIENT_SOURCES = frozenset({
    SKILL_INLINE_SOURCE,
    SKILL_LISTING_SOURCE,
    SKILL_RECOVERY_SOURCE,
})


@dataclass(frozen=True, slots=True)
class InlineSkillRuntime:
    """Effective inline-skill modifiers derived from active skill messages."""

    allowed_tool_names: frozenset[str] | None = None
    model_override: str | None = None
    effort: str | int | None = None


def strip_skill_context_messages(
    messages: list[dict[str, Any]],
    *,
    sources: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Remove skill meta messages by source without mutating the input list."""
    active_sources = (
        SKILL_TRANSIENT_SOURCES
        if sources is None
        else frozenset(sources)
    )
    return [
        message
        for message in messages
        if message.get("metadata", {}).get("source") not in active_sources
    ]


def resolve_skill_context_cwd(
    default_cwd: Path,
    context: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve the working directory used for skill visibility and listing."""
    if context is not None:
        raw_cwd = context.get("cwd")
        if isinstance(raw_cwd, Path):
            return raw_cwd.resolve()
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            return Path(raw_cwd).resolve()
    return default_cwd.resolve()


def collect_inline_skill_runtime(messages: list[dict[str, Any]]) -> InlineSkillRuntime:
    """Collapse active inline-skill metadata into one effective runtime view."""
    effective_allowlist: set[str] | None = None
    effective_model: str | None = None
    effective_effort: str | int | None = None

    for message in messages:
        metadata = message.get("metadata", {})
        if metadata.get("source") != SKILL_INLINE_SOURCE:
            continue

        raw_allowed_tools = metadata.get(SKILL_INLINE_ALLOWED_TOOLS_KEY)
        if raw_allowed_tools is not None:
            allowed_tools = {
                str(tool_name).strip()
                for tool_name in raw_allowed_tools
                if str(tool_name).strip()
            }
            if effective_allowlist is None:
                effective_allowlist = allowed_tools
            else:
                effective_allowlist &= allowed_tools

        raw_model = metadata.get("xxcode_skill_model")
        if raw_model:
            effective_model = str(raw_model)

        raw_effort = metadata.get("xxcode_skill_effort")
        if raw_effort is not None:
            effective_effort = raw_effort

    return InlineSkillRuntime(
        allowed_tool_names=(
            frozenset(effective_allowlist)
            if effective_allowlist is not None
            else None
        ),
        model_override=effective_model,
        effort=effective_effort,
    )


__all__ = [
    "InlineSkillRuntime",
    "SKILL_TRANSIENT_SOURCES",
    "collect_inline_skill_runtime",
    "resolve_skill_context_cwd",
    "strip_skill_context_messages",
]
