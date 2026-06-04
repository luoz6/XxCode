"""Skill invocation persistence for post-compact recovery."""

from __future__ import annotations

import time
from dataclasses import dataclass

POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
CHARS_PER_TOKEN_ESTIMATE = 4
SKILL_RECOVERY_SOURCE = "skill_recovery"
SKILL_RECOVERY_META_KEY = "xxcode_skill_recovery"
SKILL_RECOVERY_SNAPSHOT_VERSION = 1
_RECOVERY_HEADER = (
    "<system-reminder>\n"
    "The following skills were previously invoked and may still be relevant:\n\n"
)
_RECOVERY_FOOTER = "\n</system-reminder>"
_RECOVERY_SECTION_SEPARATOR = "\n\n---\n\n"


@dataclass(slots=True)
class InvokedSkillRecord:
    name: str
    path: str
    content: str
    invoked_at: float
    agent_scope: str
    last_turn_index: int = 0
    invocation_count: int = 1


class SkillPersistence:
    """Track skill prompts so they can be recovered after compaction."""

    def __init__(self):
        self._records: dict[str, dict[str, InvokedSkillRecord]] = {}

    def record_invocation(
        self,
        agent_scope: str,
        name: str,
        path: str,
        content: str,
        *,
        turn_count: int = 0,
    ) -> None:
        if not content:
            return

        scope_records = self._records.setdefault(agent_scope, {})
        existing = scope_records.get(name)
        now = time.time()
        if existing is None:
            scope_records[name] = InvokedSkillRecord(
                name=name,
                path=path,
                content=content,
                invoked_at=now,
                agent_scope=agent_scope,
                last_turn_index=turn_count,
                invocation_count=1,
            )
            return

        existing.path = path
        existing.content = content
        existing.invoked_at = now
        existing.last_turn_index = turn_count
        existing.invocation_count += 1

    def build_recovery_attachment(self, agent_scope: str) -> str | None:
        records = list(self._records.get(agent_scope, {}).values())
        if not records:
            return None

        total_chars_budget = POST_COMPACT_SKILLS_TOKEN_BUDGET * CHARS_PER_TOKEN_ESTIMATE
        max_chars_per_skill = POST_COMPACT_MAX_TOKENS_PER_SKILL * CHARS_PER_TOKEN_ESTIMATE

        candidates = self._build_candidates(records, max_chars_per_skill)
        selected = self._select_records(candidates, total_chars_budget)
        if not selected:
            return None

        parts: list[str] = []
        for _record, section in selected:
            parts.append(section)

        return _RECOVERY_HEADER + _RECOVERY_SECTION_SEPARATOR.join(parts) + _RECOVERY_FOOTER

    def build_recovery_message(self, agent_scope: str) -> dict[str, object] | None:
        text = self.build_recovery_attachment(agent_scope)
        if not text:
            return None
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "isMeta": True,
            "metadata": {
                "source": SKILL_RECOVERY_SOURCE,
                SKILL_RECOVERY_META_KEY: True,
                "agent_scope": agent_scope,
            },
        }

    def clear_for_scope(self, agent_scope: str) -> None:
        self._records.pop(agent_scope, None)

    def clear_all(self) -> None:
        self._records.clear()

    def export_snapshot(self) -> dict[str, object]:
        agent_scopes: dict[str, dict[str, dict[str, object]]] = {}
        for agent_scope, records in self._records.items():
            agent_scopes[agent_scope] = {
                name: {
                    "name": record.name,
                    "path": record.path,
                    "content": record.content,
                    "invoked_at": record.invoked_at,
                    "agent_scope": record.agent_scope,
                    "last_turn_index": record.last_turn_index,
                    "invocation_count": record.invocation_count,
                }
                for name, record in records.items()
            }
        return {
            "version": SKILL_RECOVERY_SNAPSHOT_VERSION,
            "agent_scopes": agent_scopes,
        }

    def import_snapshot(self, snapshot: dict[str, object] | None) -> None:
        if not snapshot:
            self.clear_all()
            return

        if snapshot.get("version") != SKILL_RECOVERY_SNAPSHOT_VERSION:
            return

        raw_scopes = snapshot.get("agent_scopes", {})
        if not isinstance(raw_scopes, dict):
            return

        restored: dict[str, dict[str, InvokedSkillRecord]] = {}
        for agent_scope, raw_records in raw_scopes.items():
            if not isinstance(agent_scope, str) or not isinstance(raw_records, dict):
                continue
            scope_records: dict[str, InvokedSkillRecord] = {}
            for name, raw_record in raw_records.items():
                if not isinstance(name, str) or not isinstance(raw_record, dict):
                    continue
                content = str(raw_record.get("content", "") or "")
                if not content:
                    continue
                try:
                    invoked_at = float(raw_record.get("invoked_at", 0.0) or 0.0)
                    last_turn_index = int(raw_record.get("last_turn_index", 0) or 0)
                    invocation_count = max(
                        1,
                        int(raw_record.get("invocation_count", 1) or 1),
                    )
                except (TypeError, ValueError):
                    continue
                scope_records[name] = InvokedSkillRecord(
                    name=str(raw_record.get("name", name) or name),
                    path=str(raw_record.get("path", "") or ""),
                    content=content,
                    invoked_at=invoked_at,
                    agent_scope=str(raw_record.get("agent_scope", agent_scope) or agent_scope),
                    last_turn_index=last_turn_index,
                    invocation_count=invocation_count,
                )
            if scope_records:
                restored[agent_scope] = scope_records

        self._records = restored

    @staticmethod
    def _truncate_to_char_limit(content: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(content) <= max_chars:
            return content
        if max_chars <= 3:
            return "." * max_chars
        return content[: max_chars - 3] + "..."

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, (len(content) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE)

    def _build_candidates(
        self,
        records: list[InvokedSkillRecord],
        max_chars_per_skill: int,
    ) -> list[tuple[InvokedSkillRecord, str]]:
        candidates: list[tuple[InvokedSkillRecord, str]] = []
        for record in records:
            header = (
                f"## Skill: {record.name}\n"
                f"Path: {record.path}\n\n"
            )
            max_content_chars = max(0, max_chars_per_skill - len(header))
            content = self._truncate_to_char_limit(
                record.content,
                max_content_chars,
            )
            section = header + content
            # Extremely long names/paths can exceed the section budget by
            # themselves, so keep a final guardrail here.
            if len(section) > max_chars_per_skill:
                section = section[:max_chars_per_skill]
            candidates.append((record, section))
        return candidates

    def _select_records(
        self,
        candidates: list[tuple[InvokedSkillRecord, str]],
        total_chars_budget: int,
    ) -> list[tuple[InvokedSkillRecord, str]]:
        if not candidates:
            return []

        fixed_overhead = len(_RECOVERY_HEADER) + len(_RECOVERY_FOOTER)
        if fixed_overhead >= total_chars_budget:
            return []

        latest_turn = max(
            (record.last_turn_index for record, _section in candidates),
            default=0,
        )
        ranked = sorted(
            candidates,
            key=lambda item: self._selection_key(item[0], item[1], latest_turn),
            reverse=True,
        )

        selected: list[tuple[InvokedSkillRecord, str]] = []
        total_chars = fixed_overhead
        for record, section in ranked:
            separator_chars = len(_RECOVERY_SECTION_SEPARATOR) if selected else 0
            next_total = total_chars + separator_chars + len(section)
            if next_total > total_chars_budget:
                continue
            selected.append((record, section))
            total_chars = next_total

        return selected

    def _selection_key(
        self,
        record: InvokedSkillRecord,
        section: str,
        latest_turn: int,
    ) -> tuple[float, int, int, float]:
        turn_gap = max(0, latest_turn - record.last_turn_index)
        recency_score = 1 / (1 + turn_gap)
        frequency_score = min(record.invocation_count, 5)
        value_score = (4 * recency_score) + frequency_score
        estimated_tokens = max(self._estimate_tokens(section), 1)
        density_score = value_score / estimated_tokens
        return (
            density_score,
            record.last_turn_index,
            record.invocation_count,
            record.invoked_at,
        )


__all__ = [
    "CHARS_PER_TOKEN_ESTIMATE",
    "InvokedSkillRecord",
    "POST_COMPACT_MAX_TOKENS_PER_SKILL",
    "POST_COMPACT_SKILLS_TOKEN_BUDGET",
    "SKILL_RECOVERY_META_KEY",
    "SKILL_RECOVERY_SOURCE",
    "SkillPersistence",
]
