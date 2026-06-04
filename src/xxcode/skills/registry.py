"""Skill registry and visibility filtering."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

from .models import SkillSpec


@dataclass(frozen=True, slots=True)
class _PatternSpec:
    anchored: bool
    directory_only: bool
    has_slash: bool
    negated: bool
    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PathContext:
    match_parts: tuple[str, ...]
    root_relative_parts: tuple[str, ...] | None
    directory_ancestors: tuple[tuple[str, ...], ...]


class SkillRegistry:
    """Central registry for loaded skills."""

    def __init__(self, root: Path | None = None) -> None:
        self._skills: dict[str, SkillSpec] = {}
        self._root = root.resolve() if root is not None else None

    def register(self, skill: SkillSpec) -> None:
        self._skills.setdefault(skill.canonical_name.lower(), skill)

    def find(self, name: str) -> SkillSpec | None:
        return self._skills.get(name.lower())

    def find_visible(self, name: str, cwd: Path) -> SkillSpec | None:
        skill = self.find(name)
        if skill is None:
            return None
        if not self.is_visible(skill, cwd):
            return None
        return skill

    def list_all(self) -> list[SkillSpec]:
        return list(self._skills.values())

    def list_user_invocable(self, cwd: Path | None = None) -> list[SkillSpec]:
        skills = self.list_all() if cwd is None else self.list_visible(cwd)
        return [skill for skill in skills if skill.frontmatter.user_invocable]

    def list_model_invocable(self, cwd: Path | None = None) -> list[SkillSpec]:
        skills = self.list_all() if cwd is None else self.list_visible(cwd)
        return [
            skill
            for skill in skills
            if not skill.frontmatter.disable_model_invocation
        ]

    def is_visible(self, skill: SkillSpec, cwd: Path) -> bool:
        patterns = skill.frontmatter.paths
        if patterns is None:
            return True

        cwd_path = cwd.resolve()
        context = self._build_path_context(cwd_path, self._root)
        return self._evaluate_patterns(context, patterns)

    def list_visible(self, cwd: Path) -> list[SkillSpec]:
        visible: list[SkillSpec] = []
        for skill in self._skills.values():
            if self.is_visible(skill, cwd):
                visible.append(skill)
        return visible

    def _evaluate_patterns(
        self,
        context: _PathContext,
        patterns: list[str],
    ) -> bool:
        visible = False
        for raw_pattern in patterns:
            spec = self._parse_pattern(raw_pattern)
            if spec is None:
                continue
            matched = self._matches_pattern(context, spec)
            if matched:
                visible = not spec.negated
        return visible

    @classmethod
    def _matches_pattern(cls, context: _PathContext, spec: _PatternSpec) -> bool:
        if spec.directory_only:
            return any(
                cls._matches_path_parts(parts, spec)
                for parts in context.directory_ancestors
            )
        return cls._matches_path_parts(context.match_parts, spec)

    @staticmethod
    def _parse_pattern(pattern: str) -> _PatternSpec | None:
        normalized = pattern.replace("\\", "/").strip()
        if not normalized:
            return None
        negated = normalized.startswith("!")
        if negated:
            normalized = normalized[1:].strip()
        normalized = normalized.removeprefix("./")
        anchored = normalized.startswith("/")
        directory_only = normalized.endswith("/")
        normalized = normalized.strip("/")
        if not normalized:
            return None
        parts = tuple(part for part in normalized.split("/") if part)
        if not parts:
            return None
        return _PatternSpec(
            anchored=anchored,
            directory_only=directory_only,
            has_slash="/" in normalized,
            negated=negated,
            parts=parts,
        )

    @staticmethod
    def _build_path_context(path: Path, root: Path | None) -> _PathContext:
        normalized_parts = tuple(
            part for part in path.parts if part not in (path.anchor, "")
        )
        root_relative_parts: tuple[str, ...] | None = None
        if root is not None:
            try:
                relative = path.relative_to(root)
            except ValueError:
                root_relative_parts = None
            else:
                root_relative_parts = tuple(
                    part for part in relative.parts if part
                )

        match_parts = (
            root_relative_parts
            if root_relative_parts is not None
            else normalized_parts
        )
        directory_ancestors = SkillRegistry._directory_ancestors(path, root)
        return _PathContext(
            match_parts=match_parts,
            root_relative_parts=root_relative_parts,
            directory_ancestors=directory_ancestors,
        )

    @classmethod
    def _matches_path_parts(
        cls,
        path_parts: tuple[str, ...],
        spec: _PatternSpec,
    ) -> bool:
        if spec.anchored:
            return cls._segments_match(path_parts, spec.parts)

        if not spec.has_slash:
            return any(
                fnmatchcase(segment, spec.parts[0])
                for segment in path_parts
            )

        return cls._segments_match(path_parts, spec.parts)

    @staticmethod
    def _directory_ancestors(
        path: Path,
        root: Path | None,
    ) -> tuple[tuple[str, ...], ...]:
        if not path.exists():
            return tuple()

        current = path if path.is_dir() else path.parent
        ancestors: list[tuple[str, ...]] = []
        while True:
            if root is not None:
                try:
                    relative = current.relative_to(root)
                except ValueError:
                    break
                parts = tuple(part for part in relative.parts if part)
            else:
                parts = tuple(
                    part for part in current.parts
                    if part not in (current.anchor, "")
                )
            if parts:
                ancestors.append(parts)
            if current.parent == current:
                break
            if root is not None and current == root:
                break
            current = current.parent
        return tuple(ancestors)

    @staticmethod
    @lru_cache(maxsize=2048)
    def _segments_match(
        path_parts: tuple[str, ...],
        pattern_parts: tuple[str, ...],
    ) -> bool:
        @lru_cache(maxsize=None)
        def _match(pattern_index: int, path_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)

            token = pattern_parts[pattern_index]
            if token == "**":
                return (
                    _match(pattern_index + 1, path_index)
                    or (
                        path_index < len(path_parts)
                        and _match(pattern_index, path_index + 1)
                    )
                )

            if path_index >= len(path_parts):
                return False
            if not fnmatchcase(path_parts[path_index], token):
                return False
            return _match(pattern_index + 1, path_index + 1)

        return _match(0, 0)


__all__ = ["SkillRegistry"]
