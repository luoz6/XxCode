"""Session persistence - save/load conversations as JSONL files."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionMeta:
    """Metadata for a saved session."""

    session_id: str
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    message_count: int = 0
    turn_count: int = 0


class SessionStore:
    """Manages session persistence using JSONL files."""

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.jsonl"

    def _meta_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.meta.json"

    def _state_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.state.json"

    def _skill_recovery_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.skill-recovery.json"

    def _skill_recovery_commit_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.skill-recovery.commit.json"

    def _task_runtime_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.tasks.json"

    @staticmethod
    def _atomic_replace_text(path: Path, content: str) -> None:
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def _atomic_write_json(cls, path: Path, data: dict[str, Any]) -> None:
        cls._atomic_replace_text(
            path,
            json.dumps(data, ensure_ascii=False, indent=2),
        )

    @classmethod
    def _atomic_write_jsonl(cls, path: Path, records: list[dict[str, Any]]) -> None:
        cls._atomic_replace_text(
            path,
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        )

    @staticmethod
    def _load_json_file(path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None

    @classmethod
    def _load_json_mapping(cls, path: Path) -> dict[str, Any] | None:
        data = cls._load_json_file(path)
        if not isinstance(data, dict):
            return None
        return data

    def _write_meta(self, meta: SessionMeta) -> None:
        """Write session metadata to disk."""
        self._atomic_write_json(self._meta_path(meta.session_id), meta.__dict__)

    def _upsert_meta(
        self,
        session_id: str,
        *,
        message_count: int | None = None,
        turn_count: int | None = None,
        meta: SessionMeta | None = None,
    ) -> SessionMeta:
        """Create or update metadata without dropping existing counters."""
        current = meta or self.load_meta(session_id)
        if current is None:
            current = SessionMeta(session_id=session_id)

        current.last_updated = time.time()
        if message_count is not None:
            current.message_count = message_count
        if turn_count is not None:
            current.turn_count = turn_count

        self._write_meta(current)
        return current

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        meta: SessionMeta | None = None,
        turn_count: int | None = None,
    ) -> None:
        """Save messages to a session file (JSONL format, one message per line)."""
        self._atomic_write_jsonl(self._session_path(session_id), messages)
        self._upsert_meta(
            session_id,
            message_count=len(messages),
            turn_count=turn_count,
            meta=meta,
        )

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages from a session file."""
        path = self._session_path(session_id)
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict):
                        messages.append(payload)
        except OSError:
            return []
        return messages

    def load_meta(self, session_id: str) -> SessionMeta | None:
        """Load session metadata."""
        data = self._load_json_mapping(self._meta_path(session_id))
        if data is None:
            return None
        try:
            return SessionMeta(**data)
        except (TypeError, ValueError):
            return None

    def list_sessions(self) -> list[SessionMeta]:
        """List all saved sessions, most recent first."""
        sessions: list[SessionMeta] = []
        for meta_path in sorted(
            self.session_dir.glob("*.meta.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            meta = self.load_meta(meta_path.stem.rsplit(".meta", 1)[0])
            if meta is not None:
                sessions.append(meta)
        return sessions

    def delete(self, session_id: str) -> None:
        """Delete a session."""
        self._session_path(session_id).unlink(missing_ok=True)
        self._meta_path(session_id).unlink(missing_ok=True)
        self._state_path(session_id).unlink(missing_ok=True)
        self._skill_recovery_path(session_id).unlink(missing_ok=True)
        self._skill_recovery_commit_path(session_id).unlink(missing_ok=True)
        self._task_runtime_path(session_id).unlink(missing_ok=True)

    def save_state(self, session_id: str, state: "AgentState") -> None:
        """Persist full AgentState without discarding an existing recovery snapshot."""
        self.save_state_with_recovery(
            session_id,
            state,
            self.load_skill_recovery(session_id),
            self.load_task_runtime_snapshot(session_id),
        )

    def save_state_with_recovery(
        self,
        session_id: str,
        state: "AgentState",
        recovery_snapshot: dict[str, object] | None = None,
        task_runtime_snapshot: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist state plus a recovery snapshot using conservative generation checks.

        Each file write is atomic on its own, but the four-file publish is not a
        single transaction. On crash, a newer state file may become visible while
        paired recovery data is later discarded by generation mismatch.
        """
        generation = uuid.uuid4().hex
        state_payload = {
            "generation": generation,
            "state": state.to_dict(),
        }
        recovery_payload: dict[str, object] = {
            "version": 1,
            "generation": generation,
            "agent_scopes": {},
        }
        if recovery_snapshot:
            recovery_payload.update(recovery_snapshot)
            recovery_payload["generation"] = generation

        self._atomic_write_json(self._state_path(session_id), state_payload)
        self._atomic_write_json(self._skill_recovery_path(session_id), recovery_payload)
        self._atomic_write_json(
            self._task_runtime_path(session_id),
            {
                "version": 1,
                "generation": generation,
                "tasks": task_runtime_snapshot or [],
            },
        )
        self._atomic_write_json(
            self._skill_recovery_commit_path(session_id),
            {"generation": generation, "version": 1},
        )
        self._upsert_meta(
            session_id,
            message_count=len(state.messages),
            turn_count=state.turn_count,
        )

    def load_state(self, session_id: str) -> "AgentState | None":
        """Load full AgentState from disk. Returns None if no state file exists."""
        from ..agent.state import AgentState

        data = self._load_json_mapping(self._state_path(session_id))
        if data is None:
            return None
        payload = data["state"] if isinstance(data.get("state"), dict) else data
        if not isinstance(payload, dict):
            return None
        try:
            return AgentState.from_dict(payload)
        except Exception:
            return None

    def load_skill_recovery(self, session_id: str) -> dict[str, object] | None:
        """Load recovery data only when state, recovery, and commit generations align."""
        state_path = self._state_path(session_id)
        recovery_path = self._skill_recovery_path(session_id)
        commit_path = self._skill_recovery_commit_path(session_id)
        if not state_path.exists() or not recovery_path.exists() or not commit_path.exists():
            return None

        state_data = self._load_json_mapping(state_path)
        recovery_data = self._load_json_mapping(recovery_path)
        commit_data = self._load_json_mapping(commit_path)
        if state_data is None or recovery_data is None or commit_data is None:
            return None

        state_generation = state_data.get("generation")
        recovery_generation = recovery_data.get("generation")
        commit_generation = commit_data.get("generation")
        if (
            not isinstance(state_generation, str)
            or not isinstance(recovery_generation, str)
            or not isinstance(commit_generation, str)
            or state_generation != recovery_generation
            or state_generation != commit_generation
            or recovery_data.get("version") != 1
        ):
            return None
        return recovery_data

    def load_task_runtime_snapshot(self, session_id: str) -> list[dict[str, Any]] | None:
        """Load task runtime snapshot when generations align."""
        state_path = self._state_path(session_id)
        task_path = self._task_runtime_path(session_id)
        commit_path = self._skill_recovery_commit_path(session_id)
        if not state_path.exists() or not task_path.exists() or not commit_path.exists():
            return None

        state_data = self._load_json_mapping(state_path)
        task_data = self._load_json_mapping(task_path)
        commit_data = self._load_json_mapping(commit_path)
        if state_data is None or task_data is None or commit_data is None:
            return None

        state_generation = state_data.get("generation")
        task_generation = task_data.get("generation")
        commit_generation = commit_data.get("generation")
        tasks = task_data.get("tasks")
        if (
            not isinstance(state_generation, str)
            or not isinstance(task_generation, str)
            or not isinstance(commit_generation, str)
            or state_generation != task_generation
            or state_generation != commit_generation
            or task_data.get("version") != 1
            or not isinstance(tasks, list)
        ):
            return None
        return tasks

