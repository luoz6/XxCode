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
from .ui_shared import (
    DISPLAY_RISK_LABELS,
    PHASE1_PERMISSION_ACTION_LABELS,
    build_session_toolbar,
    detect_display_mode,
    get_display_symbols,
    normalize_permission_answer,
    translate_backend_risk_level,
)

logger = logging.getLogger(__name__)


class PromptToolkitFullscreenUI:
    """Minimal frame-driven full-screen prototype."""

    uses_frame_transcript = True
    TRANSCRIPT_VIEWPORT_LINES = 18
    TASKS_VIEWPORT_LINES = 8
    MODAL_VIEWPORT_LINES = 8

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
        self._transcript_text_cache = ""
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
        self._focused_pane = "input"
        self._pane_scroll_offsets = {
            "transcript": 0,
            "tasks": 0,
            "modal": 0,
        }
        self._transcript_search_query = ""
        self._transcript_search_matches: list[int] = []
        self._transcript_search_index = -1
        self._thinking_expanded = False
        self._task_activity_expanded = False

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
        self._transcript_text_cache = ""
        self._rendered_transcript_blocks = []
        self._task_chunks = []
        self._rendered_task_blocks = []
        self._tasks_text = ""
        self._modal_text = ""
        self._status_text = ""
        self._input_enabled = True
        self._input_placeholder = ""
        self._focused_pane = "input"
        self._pane_scroll_offsets = {
            "transcript": 0,
            "tasks": 0,
            "modal": 0,
        }
        self._transcript_search_query = ""
        self._transcript_search_matches = []
        self._transcript_search_index = -1
        self._thinking_expanded = False
        self._task_activity_expanded = False
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
        self._focused_pane = "modal"
        self._status_text = self._format_status(self._last_frame)
        self._refresh_controls()

    def clear_modal(self) -> None:
        self._active_modal_state = None
        self._modal_text = ""
        self._focused_pane = "input" if self._input_enabled else "transcript"
        self._status_text = self._format_status(self._last_frame)
        self._refresh_controls()

    def _visible_panes(self) -> list[str]:
        panes = ["transcript", "tasks"]
        if self._modal_text:
            panes.append("modal")
        panes.append("input")
        return panes

    def _cycle_focus(self, direction: int) -> None:
        panes = self._visible_panes()
        if not panes:
            return
        if self._focused_pane not in panes:
            self._focused_pane = panes[-1]
            return
        index = panes.index(self._focused_pane)
        self._focused_pane = panes[(index + direction) % len(panes)]

    @staticmethod
    def _slice_viewport_text(text: str, *, offset: int, height: int) -> str:
        lines = text.splitlines()
        if not lines:
            return ""
        start = max(0, offset)
        end = max(start, start + max(1, height))
        return "\n".join(lines[start:end])

    def _scroll_pane(self, pane: str, *, text: str, delta: int, height: int) -> None:
        lines = text.splitlines()
        max_offset = max(0, len(lines) - max(1, height))
        current = self._pane_scroll_offsets.get(pane, 0)
        next_offset = max(0, min(max_offset, current + delta))
        self._pane_scroll_offsets[pane] = next_offset

    def _scroll_active_pane(self, delta: int) -> None:
        if self._focused_pane == "transcript":
            self._scroll_pane(
                "transcript",
                text=self._transcript_text_cache,
                delta=delta,
                height=self.TRANSCRIPT_VIEWPORT_LINES,
            )
        elif self._focused_pane == "tasks":
            self._scroll_pane(
                "tasks",
                text=self._tasks_text,
                delta=delta,
                height=self.TASKS_VIEWPORT_LINES,
            )
        elif self._focused_pane == "modal":
            self._scroll_pane(
                "modal",
                text=self._modal_text,
                delta=delta,
                height=self.MODAL_VIEWPORT_LINES,
            )

    def _page_scroll_delta(self) -> int:
        if self._focused_pane == "transcript":
            return self.TRANSCRIPT_VIEWPORT_LINES
        if self._focused_pane == "tasks":
            return self.TASKS_VIEWPORT_LINES
        if self._focused_pane == "modal":
            return self.MODAL_VIEWPORT_LINES
        return self.TRANSCRIPT_VIEWPORT_LINES

    def _set_transcript_search_query(self, query: str) -> None:
        normalized = (query or "").strip()
        self._transcript_search_query = normalized
        if not normalized:
            self._transcript_search_matches = []
            self._transcript_search_index = -1
            return

        matches: list[int] = []
        for idx, line in enumerate(self._transcript_text_cache.splitlines()):
            if normalized.lower() in line.lower():
                matches.append(idx)
        self._transcript_search_matches = matches
        self._transcript_search_index = 0 if matches else -1

    def _step_transcript_search(self, direction: int) -> None:
        if not self._transcript_search_matches:
            self._transcript_search_index = -1
            return
        count = len(self._transcript_search_matches)
        current = self._transcript_search_index
        if current < 0:
            current = 0
        self._transcript_search_index = (current + direction) % count
        match_line = self._transcript_search_matches[self._transcript_search_index]
        self._pane_scroll_offsets["transcript"] = max(
            0,
            match_line - self.TRANSCRIPT_VIEWPORT_LINES // 2,
        )

    def _cancel_search_mode(self) -> None:
        self._input_mode = "normal"
        self._input_placeholder = (
            "请输入消息并回车。"
            if self._input_enabled
            else "正在等待代理继续执行。"
        )

    def _enter_search_mode(self) -> None:
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            return
        if self._pending_selection_future is not None and not self._pending_selection_future.done():
            return
        if not self._input_enabled:
            return
        self._input_mode = "search_query"
        self._input_enabled = True
        self._input_placeholder = "输入搜索词并回车。"

    def _toggle_thinking_visibility(self) -> None:
        self._thinking_expanded = not self._thinking_expanded
        self._rendered_transcript_blocks = []
        self._refresh_from_frame(self._last_frame)

    def _toggle_task_activity_detail(self) -> None:
        self._task_activity_expanded = not self._task_activity_expanded
        self._rendered_task_blocks = []
        self._refresh_from_frame(self._last_frame)

    def _focus_marker(self) -> str:
        symbols = get_display_symbols(
            detect_display_mode(
                getattr(getattr(self.console, "file", None), "encoding", None)
            )
        )
        return symbols["marker.pointer"]

    def _render_region(self, title: str, body: str, *, pane: str) -> str:
        focused = self._focused_pane == pane
        marker = self._focus_marker() if focused else " "
        if body:
            return f"{marker} {title}\n{body}"
        return f"{marker} {title}"

    def _render_transcript_region(self) -> str:
        body = self._slice_viewport_text(
            self._transcript_text_cache,
            offset=self._pane_scroll_offsets["transcript"],
            height=self.TRANSCRIPT_VIEWPORT_LINES,
        )
        return self._render_region("对话记录", body, pane="transcript")

    def _render_tasks_region(self) -> str:
        body = self._slice_viewport_text(
            self._tasks_text,
            offset=self._pane_scroll_offsets["tasks"],
            height=self.TASKS_VIEWPORT_LINES,
        )
        return self._render_region("任务概览", body, pane="tasks")

    def _render_modal_region(self) -> str:
        body = self._slice_viewport_text(
            self._modal_text,
            offset=self._pane_scroll_offsets["modal"],
            height=self.MODAL_VIEWPORT_LINES,
        )
        return self._render_region("权限请求", body, pane="modal")

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
        self._input_placeholder = "请输入消息并回车。"
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
        self._input_placeholder = "请输入 y / a / n。"
        loop = asyncio.get_running_loop()
        self._pending_permission_future = loop.create_future()
        tool_input = getattr(tc, "input", {}) or {}
        backend_risk_level = tool_risk_level(getattr(tc, "name", ""), tool_input)
        display_risk_level = translate_backend_risk_level(backend_risk_level)
        target_summary = tool_input.get("file_path", "") or tool_input.get("command", "")
        summary_lines: list[str] = []
        if getattr(tc, "name", "") == "run_shell":
            summary_lines = [str(tool_input.get("command", ""))[:200]]
        elif getattr(tc, "name", "") in ("write_file", "edit_file"):
            summary_lines = [str(tool_input.get("content", ""))[:200]]
        self.show_modal(
            {
                "kind": "tool_permission",
                "tool_name": getattr(tc, "name", ""),
                "target_summary": str(target_summary),
                "backend_risk_level": backend_risk_level,
                "display_risk_level": display_risk_level,
                "dangerous": dangerous,
                "summary_lines": [line for line in summary_lines if line],
                "action_labels": list(PHASE1_PERMISSION_ACTION_LABELS),
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
        self._input_placeholder = "请输入编号并回车。"
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

        @kb.add("f6")
        def _focus_next(event) -> None:
            self._cycle_focus(1)
            self._refresh_controls()

        @kb.add("s-f6")
        def _focus_prev(event) -> None:
            self._cycle_focus(-1)
            self._refresh_controls()

        @kb.add("pageup")
        def _page_up(event) -> None:
            self._scroll_active_pane(-self._page_scroll_delta())
            self._refresh_controls()

        @kb.add("pagedown")
        def _page_down(event) -> None:
            self._scroll_active_pane(self._page_scroll_delta())
            self._refresh_controls()

        @kb.add("c-f")
        def _start_search(event) -> None:
            self._enter_search_mode()
            self._refresh_controls()

        @kb.add("f3")
        def _search_next(event) -> None:
            self._step_transcript_search(1)
            self._refresh_controls()

        @kb.add("s-f3")
        def _search_prev(event) -> None:
            self._step_transcript_search(-1)
            self._refresh_controls()

        @kb.add("f4")
        def _toggle_thinking(event) -> None:
            self._toggle_thinking_visibility()

        @kb.add("f5")
        def _toggle_task_detail(event) -> None:
            self._toggle_task_activity_detail()

        @kb.add("escape", eager=True)
        def _escape_modal(event) -> None:
            if self._active_modal_state is not None:
                self._focused_pane = "input"
                self._refresh_controls()
                return
            if self._input_mode == "search_query":
                self._cancel_search_mode()
                self._refresh_controls()

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
        elif self._input_mode == "search_query":
            self._set_transcript_search_query(text)
            self._cancel_search_mode()
            self._refresh_controls()
            return False
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
        self._transcript_control.text = self._render_transcript_region()
        self._tasks_control.text = self._render_tasks_region()
        self._modal_control.text = self._render_modal_region()
        self._status_control.text = self._status_text
        self._input_meta_control.text = self._format_input_meta()
        self._input_field.buffer.read_only = not self._is_input_editable()
        app = self._app
        if app is not None and app.is_running:
            app.invalidate()

    def _append_transcript(self, text: str) -> None:
        safe = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        self._transcript_chunks.append(safe)
        self._transcript_text_cache = "".join(self._transcript_chunks)
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
        self._transcript_text_cache = "".join(self._transcript_chunks)

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

    def _format_transcript_block(self, block: Dict[str, Any]) -> str:
        kind = block.get("kind", "")
        text = str(block.get("text", ""))
        if kind == "assistant":
            return text
        if kind == "thinking":
            if not self._thinking_expanded:
                return "[thinking collapsed]"
            return "[thinking] " + text
        if kind == "tool_call":
            return text + "\n"
        if kind == "tool_result":
            normalized = text.removeprefix("[tool result] ").removeprefix("[tool result]")
            normalized = normalized.strip()
            if not normalized:
                return "工具结果:\n"
            return "工具结果: " + normalized + "\n"
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
                if self._task_activity_expanded and entry.get("result_text"):
                    blocks.append(
                        {
                            "kind": "activity_detail",
                            "key": "{}:{}:detail".format(entry.get("task_id", ""), index),
                            "text": "  result: {}".format(str(entry.get("result_text", ""))[:200]),
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
            return "选择输入：{}".format(
                self._input_placeholder or "请输入编号并回车。"
            )
        if self._pending_permission_future is not None and not self._pending_permission_future.done():
            return "权限输入：{}".format(
                self._input_placeholder or "请输入 y / a / n。"
            )
        if self._pending_input_future is not None and not self._pending_input_future.done():
            return "输入就绪：{}".format(
                self._input_placeholder or "请输入消息并回车。"
            )
        if not self._input_enabled:
            return "输入已锁定：{}".format(
                self._input_placeholder or "正在等待代理继续执行。"
            )
        return "输入就绪：{}".format(
            self._input_placeholder or "请输入消息并回车。"
        )

    def _format_status(self, frame: RenderFrame) -> str:
        if self._active_modal_state is not None:
            return "权限请求待处理"
        if frame.errors:
            return frame.errors[-1]
        if frame.warnings:
            return frame.warnings[-1]
        return self._status_text

    @staticmethod
    def _format_modal(modal_state: Dict[str, Any]) -> str:
        if not modal_state:
            return ""

        display_risk = modal_state.get("display_risk_level", "medium")
        risk_label = DISPLAY_RISK_LABELS.get(display_risk, DISPLAY_RISK_LABELS["medium"])
        summary_lines = modal_state.get("summary_lines", [])
        summary_block = "\n".join(f"- {line}" for line in summary_lines if line)
        action_labels = modal_state.get("action_labels", [])
        action_text = " / ".join(action_labels) if action_labels else " / ".join(PHASE1_PERMISSION_ACTION_LABELS)

        parts = [
            "权限请求",
            f"类型: {modal_state.get('tool_name', '')}",
            f"目标: {modal_state.get('target_summary', '')}",
            f"风险: {risk_label}",
        ]
        if summary_block:
            parts.append("摘要:")
            parts.append(summary_block)
        parts.append(f"操作: {action_text}")
        parts.append("请输入 y / a / n 并回车。")
        return "\n".join(parts)

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
