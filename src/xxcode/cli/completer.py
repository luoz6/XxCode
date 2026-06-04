"""Auto-completion for built-in commands and local file paths."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document

from .commands import COMMAND_META, iter_command_completion_items


class XxCodeCompleter(Completer):
    """Slash-command + file-path completer for prompt_toolkit."""

    def __init__(self, skill_registry=None, cwd: Path | None = None) -> None:
        self._skill_registry = skill_registry
        self._cwd = cwd
        self._path_completer = PathCompleter(
            get_paths=self._get_paths,
            expanduser=True,
            file_filter=self._is_visible,
        )

    def _get_paths(self) -> list[str]:
        cwd = self._cwd or Path.cwd()
        return [str(Path(cwd))]

    @staticmethod
    def _is_visible(path: str) -> bool:
        name = Path(path).name
        return name != ".git"

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        if text.lstrip().startswith("/"):
            stripped = text.lstrip()
            seen: set[str] = set()
            for cmd, fallback_meta in iter_command_completion_items(
                skill_registry=self._skill_registry,
                cwd=self._cwd,
            ):
                if cmd in seen or not cmd.startswith(stripped):
                    continue
                seen.add(cmd)
                yield Completion(
                    cmd,
                    start_position=-len(stripped),
                    display=cmd,
                    display_meta=COMMAND_META.get(cmd, fallback_meta),
                )
            return

        yield from self._path_completer.get_completions(document, complete_event)

