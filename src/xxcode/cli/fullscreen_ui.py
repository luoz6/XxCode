"""Experimental prompt_toolkit full-screen UI backend."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import ConditionalContainer, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea
from rich.console import Console

from ..agent import StreamEvent
from ..config import Config
from ..ui.runtime import RenderFrame
from .theme import RICH_THEME, tool_risk_level
from .ui_shared import build_session_toolbar, normalize_permission_answer

logger = logging.getLogger(__name__)


class PromptToolkitFullscreenUI:
    """Minimal frame-driven full-screen prototype."""

    uses_frame_transcript = True

    def __init__(self, config: Config | None = None):
        from ..config import get_config

        self.config = config or get_config()
        self.console = Console(soft_wrap=True, highlight=False, theme=RICH_THEME)
        self._registry = None
        self._exec_context: Dict[str, Any] = {
            "cwd": str(self.config.cwd),
            "config": self.config,
        }
        self._last_frame = RenderFrame()
        self._active_modal_state: Dict[str, Any] | None = None
        self._transcript_chunks: list[str] = []
        self._rendered_transcript_blocks: list[tuple[str, str]] = []
        self._task_chunks: list[str] = []
        self._rendered_task_blocks: list[tuple[str, str, str]] = []
        self._tasks_text = ""
        self._modal_text = ""
        self._status_text = ""
        self._app: Application | None = None
        self._app_task: asyncio.Task[Any] | None = None
        self._pending_input_future: asyncio.Future[str | None] | None = None
        self._pending_permission_future: asyncio.Future[str] | None = None
        self._pending_selection_future: asyncio.Future[str | None] | None = None
        self._selection_values: list[tuple[str, str]] = []
        self._input_mode = "normal"

        history_file = Path.home() / ".xxcode" / "history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        self._transcript_control = FormattedTextControl(text="")
        self._tasks_control = FormattedTextControl(text="")
        self._modal_control = FormattedTextControl(text="")
        self._status_control = FormattedTextControl(text="")
        self._input_meta_control = FormattedTextControl(text="")
        self._input_field = TextArea(
            multiline=False,
            wrap_lines=False,
            height=1,
            history=FileHistory(str(history_file)),
            accept_handler=self._on_accept,
        )
        self._input_enabled = True
        self._input_placeholder = ""

    def render_welcome(self, session_id: str | None = None, skill_registry=None) -> None:
        self._append_transcript(
            "XxCode - experimental prompt_toolkit full-screen UI\n"
            "Use Ctrl+C to interrupt, Ctrl+D to exit.\n\n"
        )

    def set_skill_registry(self, skill_registry) -> None:
        self._registry = skill_registry

    def set_registry(self, registry: Any) -> None:
        self._registry = registry

    def set_exec_context(self, context: Dict[str, Any]) -> None:
        self._exec_context = context

    def reset_for_new_session(self) -> None:
        self._transcript_chunks = []
        self._rendered_transcript_blocks = []
        self._task_chunks = []
        self._rendered_task_blocks = []
        self._tasks_text = ""
        self._modal_text = ""
        self._status_text = ""
        self._input_enabled = True
        self._input_placeholder = ""
        self._last_frame = RenderFrame()
        self._refresh_controls()

    async def prepare_runtime(self) -> None:
        await self._ensure_app_running()

    def mount(self, initial_frame: RenderFrame) -> None:
        self._last_frame = initial_frame
        self._refresh_from_frame(initial_frame)

    def update(self, frame: RenderFrame) -> None:
        self._last_frame = frame
        self._refresh_from_frame(frame)

    def show_modal(self, modal_state) -> None:
        self._active_modal_state = dict(modal_state)
        self._modal_text = self._format_modal(modal_state)
        self._status_text = self._format_status(self._last_frame)
        self._refresh_controls()

    def clear_modal(self) -> None:
        self._active_modal_state = None
        self._modal_text = ""
        self._status_text = self._format_status(self._last_frame)
        self._refresh_controls()

    def shutdown(self, final_snapshot: RenderFrame) -> None:
        self._last_frame = final_snapshot
        self._refresh_from_frame(final_snapshot)
        app = self._app
        if app is not None and app.is_running:
            app.exit()

    def render_event(self, event: StreamEvent) -> None:
        """Fallback event rendering for paths that don't drive RenderFrame."""
        if event.type == "text":
            self._append_transcript(event.content)
        elif event.type == "thinking":
            self._append_transcript("[thinking] " + event.content)
        elif event.type == "tool_call":
            self._append_transcript("\n[tool] " + event.content + "\n")
        elif event.type == "tool_result":
            result = event.metadata.get("result", event.content)
            if event.metadata.get("denied"):
                self._append_transcript("[tool denied]\n")
            elif result:
                self._append_transcript("[tool result] " + str(result)[:200] + "\n")
        elif event.type == "error":
            self._append_transcript("\n[error] " + event.content + "\n")
        elif event.type == "cost":
            self._status_text = event.content
            self._refresh_controls()
        elif event.type == "done":
            self._append_transcript("\n")

    async def get_input(self, state=None) -> str | None:
        await self._ensure_app_running()
        self._input_enabled = True
        self._input_mode = "normal"
        self._input_placeholder = "Type a message and press Enter."
        loop = asyncio.get_running_loop()
        self._pending_input_future = loop.create_future()
        self._status_text = self._build_toolbar(state)
        self._input_field.buffer.text = ""
        self._refresh_controls()
        try:
            return await self._pending_input_future
        finally:
            self._pending_input_future = None

    async def ask_permission(self, tc, dangerous: bool = False) -> str:
        await self._ensure_app_running()
        self._input_enabled = True
        self._input_mode = "permission_modal"
        self._input_placeholder = "Answer with y/n/a/d and press Enter."
        loop = asyncio.get_running_loop()
        self._pending_permission_future = loop.create_future()
        tool_input = getattr(tc, "input", {}) or {}
        self.show_modal(
            {
                "kind": "permission_request",
                "tool_name": getattr(tc, "name", ""),
                "dangerous": dangerous,
                "risk_level": tool_risk_level(getattr(tc, "name", ""), tool_input),
                "target_summary": tool_input.get("file_path", "") or tool_input.get("command", ""),
            }
        )
        self._input_field.buffer.text = ""
        self._refresh_controls()
        try:
            answer = await self._pending_permission_future
        finally:
            self._pending_permission_future = None
            self._input_enabled = False
            self._input_mode = "normal"
            self._input_placeholder = "Agent is responding..."
            self.clear_modal()
        return answer

    async def pick_from_list(
        self, title: str, values: list[tuple[str, str]],
    ) -> str | None:
        """Show a numbered selection list and accept user input."""
        if not values:
            return None

        await self._ensure_app_running()

        lines = [title, ""]
        for i, (_key, label) in enumerate(values, 1):
            lines.append(f"  {i}. {label}")
        lines.append("")
        lines.append("Type a number and press Enter (empty to cancel).")

        self._append_transcript("\n".join(lines))

        self._input_enabled = True
        self._input_mode = "selection"
        self._input_placeholder = "Type number and press Enter."
        self._selection_values = values

        loop = asyncio.get_running_loop()
        self._pending_selection_future = loop.create_future()
        self._input_field.buffer.text = ""
        self._refresh_controls()

        try:
            text = await self._pending_selection_future
            if text and text.isdigit():
                idx = int(text) - 1
                if 0 <= idx < len(values):
                    return values[idx][0]
            return None
        finally:
            self._pending_selection_future = None
            self._selection_values = []
            self._input_mode = "normal"

    async def _ensure_app_running(self) -> None:
        if self._app is not None and self._app.is_running:
            return
        if self._app is None:
            self._app = self._create_application()
        if self._app_task is None or self._app_task.done():
            self._app_task = asyncio.create_task(
                self._app.run_async(set_exception_handler=False),
                name="xxcode-fullscreen-ui",
            )
        await asyncio.sleep(0)

    def _create_application(self) -> Application:
        kb = KeyBindings()

        @kb.add("c-c")
        def _handle_ctrl_c(event) -> None:
            if self._pending_selection_future is not None and not self._pending_selection_future.done():
                self._pending_selection_future.set_result(None)
                self._input_field.buffer.reset()
                return
            if self._pending_input_future is not None and not self._pending_input_future.done():
                self._pending_input_future.set_result(None)
                self._input_field.buffer.reset()
                return
            if (
                self._pending_permission_future is not None
                and not self._pending_permission_future.done()
            ):
                self._pending_permission_future.set_result("no")
                self._input_field.buffer.reset()
                return
            raise KeyboardInterrupt()

        @kb.add("c-d")
        def _handle_ctrl_d(event) -> None:
            if self._pending_input_future is not None and not self._pending_input_future.done():
                self._pending_input_future.set_result(None)
                self._input_field.buffer.reset()
                return
            raise EOFError()

        root = HSplit(
            [
                Window(
                    content=self._transcript_control,
                    wrap_lines=True,
                    height=Dimension(weight=5),
                ),
                Window(
                    content=self._tasks_control,
                    wrap_lines=True,
                    height=Dimension(min=1, preferred=4),
                ),
                ConditionalContainer(
                    content=Window(
                        content=self._modal_control,
                        wrap_lines=True,
                        height=Dimension(min=1, preferred=4),
                    ),
                    filter=Condition(lambda: bool(self._modal_text)),
                ),
                Window(
                    content=self._status_control,
                    height=1,
                ),
                Window(
                    content=self._input_meta_control,
                    height=1,
                ),
                self._input_field,
            ]
        )
        return Application(
            layout=Layout(root, focused_element=self._input_field),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
        )

    def _on_accept(self, buffer) -> bool:
        text = buffer.text.strip()
        buffer.reset()
        if self._input_mode == "selection":
            if self._pending_selection_future is not None and not self._pending_selection_future.done():
                self._pending_selection_future.set_result(text)
            return False
        if self._input_mode == "permission_modal":
            if self._pending_permission_future is not None and not self._pending_permission_future.done():
                self._pending_permission_future.set_result(
                    self._normalize_permission_answer(text)
                )
        else:
            if self._pending_input_future is not None and not self._pending_input_future.done():
                self._pending_input_future.set_result(text)
        return False

    def _refresh_from_frame(self, frame: RenderFrame) -> None:
        self._sync_transcript_from_frame(frame)
        self._sync_tasks_from_frame(frame)
        self._status_text = self._format_status(frame)
        self._sync_input_from_frame(frame)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        self._transcript_control.text = self._format_transcript()
        self._tasks_control.text = self._tasks_text
        self._modal_control.text = self._modal_text
        self._status_control.text = self._status_text
        self._input_meta_control.text = self._format_input_meta()
        self._input_field.buffer.read_only = not self._is_input_editable()
        app = self._app
        if app is not None and app.is_running:
            app.invalidate()

    def _append_transcript(self, text: str) -> None:
        safe = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        self._transcript_chunks.append(safe)
        # Fallback event-driven paths bypass RenderFrame.transcript_entries.
        # Reset the rendered-block cache so the next frame-driven sync
        # can safely rebuild from authoritative frame data.
        self._rendered_transcript_blocks = []
        self._refresh_controls()

    def _sync_transcript_from_frame(self, frame: RenderFrame) -> None:
        if not frame.transcript_entries:
            return
        blocks = self._build_transcript_blocks(frame.transcript_entries)
        new_blocks = [
            self._block_signature(block)
            for block in blocks
        ]
        previous_blocks = self._rendered_transcript_blocks

        # If some other path mutated transcript chunks, fall back to
        # authoritative full rebuild from the frame.
        if previous_blocks and len(previous_blocks) != len(self._transcript_chunks):
            previous_blocks = []

        prefix_len = 0
        shared = min(len(previous_blocks), len(new_blocks))
        while prefix_len < shared and previous_blocks[prefix_len] == new_blocks[prefix_len]:
            prefix_len += 1

        if not previous_blocks:
            self._transcript_chunks = [
                self._format_transcript_block(block)
                for block in blocks
            ]
        else:
            replacement_suffix = [
                self._format_transcript_block(block)
                for block in blocks[prefix_len:]
            ]
            self._transcript_chunks = (
                self._transcript_chunks[:prefix_len]
                + replacement_suffix
            )
        self._rendered_transcript_blocks = new_blocks

    def _format_transcript(self) -> str:
        return "".join(self._transcript_chunks)

    @staticmethod
    def _build_transcript_blocks(
        entries: list[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        blocks: list[Dict[str, Any]] = []
        mergeable_kinds = ("assistant", "thinking")
        for entry in entries:
            kind = str(entry.get("kind", ""))
            text = str(entry.get("text", ""))
            if not text:
                continue
            if (
                blocks
                and kind in mergeable_kinds
                and blocks[-1].get("kind") == kind
            ):
                blocks[-1]["text"] = str(blocks[-1].get("text", "")) + text
            else:
                blocks.append({"kind": kind, "text": text})
        return blocks

    @staticmethod
    def _block_signature(block: Dict[str, Any]) -> tuple[str, str]:
        return (str(block.get("kind", "")), str(block.get("text", "")))

    @staticmethod
    def _format_transcript_block(block: Dict[str, Any]) -> str:
        kind = block.get("kind", "")
        text = str(block.get("text", ""))
        if kind == "assistant":
            return text
        if kind == "thinking":
            return "[thinking] " + text
        if kind == "tool_call":
            return text + "\n"
        if kind == "tool_result":
            return text + "\n"
        if kind == "warning":
            return "[warning] " + text + "\n"
        if kind == "error":
            return "[error] " + text + "\n"
        return text

    def _sync_tasks_from_frame(self, frame: RenderFrame) -> None:
        blocks = self._build_task_blocks(frame)
        new_blocks = [
            self._task_block_signature(block)
            for block in blocks
        ]
        previous_blocks = self._rendered_task_blocks

        if previous_blocks and len(previous_blocks) != len(self._task_chunks):
            previous_blocks = []

        prefix_len = 0
        shared = min(len(previous_blocks), len(new_blocks))
        while prefix_len < shared and previous_blocks[prefix_len] == new_blocks[prefix_len]:
            prefix_len += 1

        if not previous_blocks:
            self._task_chunks = [
                self._format_task_block(block)
                for block in blocks
            ]
        else:
            replacement_suffix = [
                self._format_task_block(block)
                for block in blocks[prefix_len:]
            ]
            self._task_chunks = self._task_chunks[:prefix_len] + replacement_suffix

        self._rendered_task_blocks = new_blocks
        self._tasks_text = self._format_tasks()

    def _build_task_blocks(self, frame: RenderFrame) -> list[Dict[str, str]]:
        blocks: list[Dict[str, str]] = []
        has_snapshots = bool(frame.tasks)
        has_activity = bool(frame.task_activity_entries)

        if not has_snapshots and not has_activity:
            return [{"kind": "empty", "key": "empty", "text": "Tasks: none"}]

        if has_snapshots:
            blocks.append(
                {"kind": "snapshot_header", "key": "snapshot_header", "text": "Task Snapshots:"}
            )
            for task_id in sorted(frame.tasks.keys()):
                record = frame.tasks[task_id]
                label = record.get("worker_label") or task_id
                status = record.get("status", "unknown")
                usage = "{}/{}".format(
                    record.get("input_tokens", 0),
                    record.get("output_tokens", 0),
                )
                tool_use_count = record.get("tool_use_count", 0)
                duration_ms = record.get("duration_ms", 0)
                blocks.append(
                    {
                        "kind": "snapshot_line",
                        "key": str(task_id),
                        "text": "- {} [{}] tools={} usage={} duration={}ms".format(
                            label,
                            status,
                            tool_use_count,
                            usage,
                            duration_ms,
                        ),
                    }
                )

        if has_activity:
            if has_snapshots:
                blocks.append({"kind": "separator", "key": "snapshot_activity", "text": ""})
            blocks.append(
                {"kind": "activity_header", "key": "activity_header", "text": "Recent Task Activity:"}
            )
            recent_entries = frame.task_activity_entries[-5:]
            for index, entry in enumerate(recent_entries):
                label = entry.get("worker_label") or entry.get("task_id", "")
                status = entry.get("status", "unknown")
                summary = entry.get("summary", "")
                blocks.append(
                    {
                        "kind": "activity_line",
                        "key": "{}:{}".format(entry.get("task_id", ""), index),
                        "text": "- {} [{}] {}".format(label, status, summary),
                    }
                )

        return blocks

    @staticmethod
    def _task_block_signature(block: Dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(block.get("kind", "")),
            str(block.get("key", "")),
            str(block.get("text", "")),
        )

    @staticmethod
    def _format_task_block(block: Dict[str, Any]) -> str:
        return str(block.get("text", ""))

    def _format_tasks(self) -> str:
        return "\n".join(self._task_chunks)

    def _sync_input_from_frame(self, frame: RenderFrame) -> None:
        if self._pending_input_future is not None and not self._pending_input_future.done():
            return
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            return
        self._input_enabled = bool(frame.input_enabled)
        self._input_mode = frame.input_mode or "normal"
        self._input_placeholder = frame.input_placeholder or ""

    def _is_input_editable(self) -> bool:
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            return True
        if self._pending_input_future is not None and not self._pending_input_future.done():
            return True
        return bool(self._input_enabled)

    def _format_input_meta(self) -> str:
        if self._pending_selection_future is not None and not self._pending_selection_future.done():
            return "Selection: {}".format(
                self._input_placeholder or "Type number and press Enter."
            )
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            return "Permission Input: {}".format(
                self._input_placeholder or "Answer with y/n/a/d and press Enter."
            )
        if self._pending_input_future is not None and not self._pending_input_future.done():
            return "Input: {}".format(
                self._input_placeholder or "Type a message and press Enter."
            )
        if not self._input_enabled:
            return "Input Disabled: {}".format(
                self._input_placeholder or "Waiting for agent."
            )
        return "Input Ready: {}".format(
            self._input_placeholder or "Type a message and press Enter."
        )

    def _format_status(self, frame: RenderFrame) -> str:
        if self._active_modal_state is not None:
            return "Permission request pending"
        if frame.errors:
            return frame.errors[-1]
        if frame.warnings:
            return frame.warnings[-1]
        return self._status_text

    @staticmethod
    def _format_modal(modal_state: Dict[str, Any]) -> str:
        if not modal_state:
            return ""
        return (
            "Permission Request\n"
            "Tool: {tool}\n"
            "Risk: {risk}\n"
            "Target: {target}\n"
            "Answer with y/n/a/d and press Enter."
        ).format(
            tool=modal_state.get("tool_name", ""),
            risk=modal_state.get("risk_level", "normal"),
            target=modal_state.get("target_summary", ""),
        )

    @staticmethod
    def _normalize_permission_answer(answer: str) -> str:
        return normalize_permission_answer(answer)

    @staticmethod
    def _build_toolbar(state) -> str:
        return build_session_toolbar(
            state,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        )
