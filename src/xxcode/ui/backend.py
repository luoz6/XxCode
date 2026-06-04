"""Compatibility backend wrappers for the existing terminal UI."""

from __future__ import annotations

from .runtime import RenderFrame


class TerminalUiBackendMixin:
    """Adds renderer-backend methods to the legacy terminal UI."""

    def mount(self, initial_frame: RenderFrame) -> None:
        self._last_frame = initial_frame
        self._last_rendered_task_signatures = {}

    def update(self, frame: RenderFrame) -> None:
        self._render_frame_updates(frame)
        self._last_frame = frame

    def show_modal(self, modal_state) -> None:
        self._active_modal_state = modal_state

    def clear_modal(self) -> None:
        self._active_modal_state = None

    def shutdown(self, final_snapshot: RenderFrame) -> None:
        self._last_frame = final_snapshot

    def _render_frame_updates(self, frame: RenderFrame) -> None:
        tasks = getattr(frame, "tasks", {})
        if not tasks:
            return

        previous = getattr(self, "_last_rendered_task_signatures", {})
        current = {}
        for task_id in sorted(tasks.keys()):
            record = tasks[task_id]
            signature = (
                record.get("status"),
                record.get("input_tokens", 0),
                record.get("output_tokens", 0),
                record.get("tool_use_count", 0),
                record.get("duration_ms", 0),
            )
            current[task_id] = signature
            if previous.get(task_id) == signature:
                continue
            self._render_task_snapshot(record)

        self._last_rendered_task_signatures = current
