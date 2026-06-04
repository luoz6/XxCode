"""Skill discovery and listing attachments."""

from __future__ import annotations

from pathlib import Path

from .models import SkillSpec
from .registry import SkillRegistry

MAX_LISTING_DESC_CHARS = 250
SKILL_LISTING_BUDGET_PCT = 0.01
CHARS_PER_TOKEN_ESTIMATE = 4
SKILL_LISTING_SOURCE = "skill_listing"
SKILL_LISTING_META_KEY = "xxcode_skill_listing"

_SOURCE_PRIORITY = {
    "user": 0,
    "project": 1,
    "bundled": 2,
}


class SkillDiscovery:
    """Build skill listing system reminders for the model."""

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def get_new_skills_attachment(
        self,
        context_window_tokens: int,
        cwd: Path | None = None,
    ) -> str | None:
        """Return a system-reminder listing of visible skills."""
        skills = self._registry.list_model_invocable(cwd) if cwd is not None else self._registry.list_model_invocable()
        if not skills:
            return None
        budget_chars = max(0, int(context_window_tokens * SKILL_LISTING_BUDGET_PCT * CHARS_PER_TOKEN_ESTIMATE))
        return self.format_listing(skills, budget_chars, preserve_bundled=True)

    def build_listing_message(
        self,
        context_window_tokens: int,
        cwd: Path | None = None,
    ) -> dict[str, object] | None:
        text = self.get_new_skills_attachment(context_window_tokens, cwd=cwd)
        if not text:
            return None
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "isMeta": True,
            "metadata": {
                "source": SKILL_LISTING_SOURCE,
                SKILL_LISTING_META_KEY: True,
            },
        }

    def format_listing(
        self,
        skills: list[SkillSpec],
        budget_chars: int,
        preserve_bundled: bool = False,
    ) -> str:
        if not skills:
            return ""

        ordered = sorted(
            skills,
            key=lambda skill: (
                _SOURCE_PRIORITY.get(str(skill.source), 99),
                skill.canonical_name.lower(),
            ),
        )
        header = (
            "<system-reminder>\n"
            "The following skills are available for use with the Skill tool:\n\n"
        )
        footer = "\n</system-reminder>"
        budget_chars = max(budget_chars, len(header) + len(footer) + 16)

        full_entries = [
            self._format_full_entry(skill)
            for skill in ordered
        ]
        full_text = header + "\n".join(full_entries) + footer
        if len(full_text) <= budget_chars:
            return full_text

        remaining = max(0, budget_chars - len(header) - len(footer) - 1)
        rendered: list[str] = []
        omitted: list[SkillSpec] = []

        protected, regular = self._partition_skills(
            ordered,
            preserve_bundled=preserve_bundled,
        )

        for skill in protected:
            entry = self._format_full_entry(skill)
            if remaining >= len(entry) + 1:
                rendered.append(entry)
                remaining -= len(entry) + 1
            else:
                omitted.append(skill)

        for skill in regular:
            entry = self._format_full_entry(skill)
            if remaining >= len(entry) + 1:
                rendered.append(entry)
                remaining -= len(entry) + 1
            else:
                omitted.append(skill)

        if omitted and remaining > 0:
            for line in self._format_names_only(omitted):
                if remaining < len(line) + 1:
                    break
                rendered.append(line)
                remaining -= len(line) + 1

        if not rendered:
            rendered = self._format_names_only(ordered)

        body = "\n".join(rendered)
        text = header + body + footer
        if len(text) > budget_chars:
            text = (header + body[: max(0, budget_chars - len(header) - len(footer))] + footer)
        return text

    def _format_full_entry(self, skill: SkillSpec) -> str:
        description = self._clip(skill.frontmatter.description, MAX_LISTING_DESC_CHARS)
        lines = [f"- {skill.canonical_name}: {description}"]
        when = skill.frontmatter.when_to_use
        if when:
            lines.append(f"  When to use: {self._clip(when, MAX_LISTING_DESC_CHARS)}")
        return "\n".join(lines)

    def _format_names_only(self, skills: list[SkillSpec]) -> list[str]:
        return [f"- {skill.canonical_name}" for skill in skills]

    @staticmethod
    def _partition_skills(
        ordered: list[SkillSpec],
        *,
        preserve_bundled: bool,
    ) -> tuple[list[SkillSpec], list[SkillSpec]]:
        if not preserve_bundled:
            return [], ordered

        protected: list[SkillSpec] = []
        regular: list[SkillSpec] = []
        for skill in ordered:
            if str(skill.source) == "bundled":
                protected.append(skill)
            else:
                regular.append(skill)
        return protected, regular

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = " ".join(text.split())
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        if limit <= 3:
            return "." * limit
        return text[: limit - 3] + "..."


__all__ = [
    "CHARS_PER_TOKEN_ESTIMATE",
    "MAX_LISTING_DESC_CHARS",
    "SKILL_LISTING_BUDGET_PCT",
    "SKILL_LISTING_META_KEY",
    "SKILL_LISTING_SOURCE",
    "SkillDiscovery",
]
