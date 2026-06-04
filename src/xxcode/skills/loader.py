"""Skill discovery and lazy loading."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import Config
from .models import SkillParseError, SkillSource, SkillSpec, parse_skill_md

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"


class SkillLoader:
    """Load bundled, user, and project skills with path de-duplication."""

    def __init__(self, config: Config):
        self._config = config
        self._seen_paths: set[str] = set()

    def load_frontmatter_only(self) -> list[SkillSpec]:
        """Load only frontmatter for all configured skill sources."""
        self._seen_paths.clear()
        skills: list[SkillSpec] = []
        skills.extend(self._load_directory(self.user_skills_dir, SkillSource.USER))
        skills.extend(self._load_directory(self.project_skills_dir, SkillSource.PROJECT))
        skills.extend(self._load_bundled_skills())
        return skills

    def load_full_content(self, skill: SkillSpec) -> SkillSpec:
        """Return a copy-like updated skill with full markdown content loaded."""
        if skill.content is not None or skill.skill_file is None:
            return skill

        frontmatter, content = parse_skill_md(skill.skill_file)
        return SkillSpec(
            frontmatter=frontmatter,
            source=skill.source,
            directory=skill.directory,
            skill_file=skill.skill_file,
            canonical_name=skill.canonical_name,
            content=content,
            loaded_at=time.time(),
        )

    @property
    def project_skills_dir(self) -> Path:
        return Path(self._config.cwd) / self._config.skills_dir

    @property
    def user_skills_dir(self) -> Path:
        return Path(self._config.user_skills_dir).expanduser()

    @property
    def bundled_skills_dir(self) -> Path:
        return Path(__file__).resolve().parent / "bundled"

    def _load_bundled_skills(self) -> list[SkillSpec]:
        # Bundled skills act as built-in defaults. They load after local
        # sources so user/project skills with the same canonical name win.
        return self._load_directory(self.bundled_skills_dir, SkillSource.BUNDLED)

    def _load_directory(self, root: Path, source: SkillSource) -> list[SkillSpec]:
        if not root.exists() or not root.is_dir():
            return []

        skills: list[SkillSpec] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue

            skill_file = child / SKILL_FILENAME
            if not skill_file.exists():
                continue

            try:
                resolved = str(skill_file.resolve())
            except OSError:
                resolved = str(skill_file.absolute())

            if resolved in self._seen_paths:
                continue

            try:
                frontmatter, _content = parse_skill_md(skill_file)
            except SkillParseError as exc:
                logger.warning("Skipping invalid skill '%s': %s", child.name, exc)
                continue

            self._seen_paths.add(resolved)
            skills.append(
                SkillSpec(
                    frontmatter=frontmatter,
                    source=source,
                    directory=child,
                    skill_file=skill_file,
                    canonical_name=child.name,
                )
            )

        return skills


__all__ = ["SKILL_FILENAME", "SkillLoader"]
