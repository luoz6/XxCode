"""Session persistence — save/load conversations as JSONL files."""

import json
import time
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

    def save(self, session_id: str, messages: list[dict[str, Any]], meta: SessionMeta | None = None) -> None:
        """Save messages to a session file (JSONL format, one message per line)."""
        path = self._session_path(session_id)
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        if meta is None:
            meta = SessionMeta(session_id=session_id, message_count=len(messages))
        meta.last_updated = time.time()
        meta.message_count = len(messages)

        meta_path = self._meta_path(session_id)
        meta_path.write_text(json.dumps(meta.__dict__, ensure_ascii=False), encoding="utf-8")

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages from a session file."""
        path = self._session_path(session_id)
        if not path.exists():
            return []

        messages = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        return messages

    def load_meta(self, session_id: str) -> SessionMeta | None:
        """Load session metadata."""
        meta_path = self._meta_path(session_id)
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return SessionMeta(**data)

    def list_sessions(self) -> list[SessionMeta]:
        """List all saved sessions, most recent first."""
        sessions: list[SessionMeta] = []
        for meta_path in sorted(self.session_dir.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = self.load_meta(meta_path.stem.rsplit(".meta", 1)[0])
            if meta:
                sessions.append(meta)
        return sessions

    def delete(self, session_id: str) -> None:
        """Delete a session."""
        self._session_path(session_id).unlink(missing_ok=True)
        self._meta_path(session_id).unlink(missing_ok=True)
