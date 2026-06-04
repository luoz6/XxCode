"""Skill models and SKILL.md parsing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class SkillSource(StrEnum):
    USER = "user"
    PROJECT = "project"
    BUNDLED = "bundled"


class SkillParseError(Exception):
    """Raised when a skill definition cannot be parsed."""


@dataclass(slots=True)
class SkillFrontmatter:
    name: str
    description: str
    when_to_use: str | None = None
    argument_hint: str | None = None
    arguments: list[str] | None = None
    context: str = "inline"
    allowed_tools: list[str] | None = None
    model: str | None = None
    effort: str | int | None = None
    agent: str | None = None
    shell: str | None = None
    paths: list[str] | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False


@dataclass(slots=True)
class SkillSpec:
    frontmatter: SkillFrontmatter
    source: SkillSource
    directory: Path | None
    skill_file: Path | None
    canonical_name: str
    content: str | None = None
    loaded_at: float | None = None


def parse_skill_md(file_path: Path) -> tuple[SkillFrontmatter, str]:
    """Read a SKILL.md file and return validated frontmatter + markdown content."""
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillParseError(f"Unable to read skill file: {file_path}") from exc

    if not raw.startswith("---"):
        raise SkillParseError(f"Missing YAML frontmatter in {file_path}")

    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise SkillParseError(f"Malformed YAML frontmatter in {file_path}")

    try:
        loaded = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"Invalid YAML frontmatter in {file_path}") from exc

    if not isinstance(loaded, dict):
        raise SkillParseError(f"Skill frontmatter must be a mapping in {file_path}")

    content = parts[2].lstrip("\r\n")
    return validate_frontmatter(loaded, file_path=file_path), content


def validate_frontmatter(raw: dict[str, Any], *, file_path: Path) -> SkillFrontmatter:
    """Validate supported frontmatter keys and coerce kebab-case names."""
    canonical = {
        key.replace("-", "_"): value
        for key, value in raw.items()
    }

    _KNOWN_KEYS = frozenset({
        "name", "description", "when_to_use", "argument_hint", "arguments",
        "context", "allowed_tools", "model", "effort", "agent", "shell",
        "paths", "user_invocable", "disable_model_invocation",
    })
    for key in canonical:
        if key not in _KNOWN_KEYS:
            logger.warning(
                "Skill '%s' has unknown frontmatter field '%s', ignoring.",
                canonical.get("name", file_path.parent.name),
                key,
            )

    name = canonical.get("name")
    if not isinstance(name, str) or not name.strip():
        name = file_path.parent.name
    name = name.strip()

    description = canonical.get("description")
    if not isinstance(description, str) or not description.strip():
        logger.warning(
            "Skill '%s' is missing a non-empty description, using directory name as fallback.",
            name,
        )
        description = name

    context = canonical.get("context", "inline")
    if context not in {"inline", "fork"}:
        logger.warning(
            "Skill '%s' has invalid context '%s' (expected inline|fork), defaulting to 'inline'.",
            name,
            context,
        )
        context = "inline"

    arguments = _coerce_string_list(
        canonical.get("arguments"),
        field_name="arguments",
        skill_name=name,
        allow_scalar=True,
        allow_empty=True,
    )
    allowed_tools = _coerce_string_list(
        canonical.get("allowed_tools"),
        field_name="allowed-tools",
        skill_name=name,
        allow_scalar=True,
        allow_empty=True,
    )
    paths = _coerce_string_list(
        canonical.get("paths"),
        field_name="paths",
        skill_name=name,
        allow_scalar=True,
        allow_empty=True,
    )

    return SkillFrontmatter(
        name=name,
        description=description.strip(),
        when_to_use=_coerce_optional_string(canonical.get("when_to_use")),
        argument_hint=_coerce_optional_string(canonical.get("argument_hint")),
        arguments=arguments,
        context=context,
        allowed_tools=allowed_tools,
        model=_coerce_model(canonical.get("model")),
        effort=_coerce_effort(canonical.get("effort"), skill_name=name),
        agent=_coerce_optional_string(canonical.get("agent")),
        shell=_coerce_optional_string(canonical.get("shell")),
        paths=paths,
        user_invocable=_coerce_bool(canonical.get("user_invocable"), default=True, skill_name=name, field_name="user-invocable"),
        disable_model_invocation=_coerce_bool(
            canonical.get("disable_model_invocation"),
            default=False,
            skill_name=name,
            field_name="disable-model-invocation",
        ),
    )


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _coerce_model(value: Any) -> str | None:
    """Coerce model field, treating 'inherit' as None (use current model)."""
    result = _coerce_optional_string(value)
    if result is not None and result.lower() == "inherit":
        return None
    return result


def _coerce_string_list(
    value: Any,
    *,
    field_name: str,
    skill_name: str,
    allow_scalar: bool = False,
    allow_empty: bool = False,
    invalid_default: list[str] | None = None,
) -> list[str] | None:
    def _invalid_result() -> list[str] | None:
        return list(invalid_default) if invalid_default is not None else None

    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if allow_scalar and stripped:
            logger.warning(
                "Skill '%s' field '%s' should be a YAML list; treating the scalar value as a one-item list.",
                skill_name,
                field_name,
            )
            return [stripped]
        logger.warning(
            "Skill '%s' field '%s' must be a string list, ignoring.",
            skill_name,
            field_name,
        )
        return _invalid_result()

    if not isinstance(value, list):
        logger.warning(
            "Skill '%s' field '%s' must be a string list, ignoring.",
            skill_name,
            field_name,
        )
        return _invalid_result()

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            logger.warning(
                "Skill '%s' field '%s' must contain only non-empty strings, ignoring.",
                skill_name,
                field_name,
            )
            return _invalid_result()
        normalized.append(item.strip())

    if not normalized and not allow_empty:
        logger.warning(
            "Skill '%s' field '%s' must not be empty, ignoring.",
            skill_name,
            field_name,
        )
        return _invalid_result()

    return normalized


def _coerce_bool(value: Any, *, default: bool, skill_name: str, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    logger.warning(
        "Skill '%s' field '%s' must be boolean, defaulting to %s.",
        skill_name,
        field_name,
        default,
    )
    return default


def _coerce_effort(value: Any, *, skill_name: str) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in {"quick", "standard"}:
            return stripped
        if stripped.isdigit():
            return int(stripped)
    logger.warning(
        "Skill '%s' field 'effort' must be quick, standard, or integer, ignoring.",
        skill_name,
    )
    return None


__all__ = [
    "SkillFrontmatter",
    "SkillParseError",
    "SkillSource",
    "SkillSpec",
    "parse_skill_md",
    "validate_frontmatter",
]
