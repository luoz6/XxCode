"""UI runtime and normalized event model for terminal sessions."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, runtime_checkable

from ..agent.events import StreamEvent


@dataclass
class UiEvent:
    """Normalized event consumed by UI runtimes/backends."""

    type: str
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskUiEvent:
    """UI-facing snapshot event emitted by the task runtime."""

    type: str
    task_id: str
    record: Dict[str, Any]
    summary: str
    result_text: str


@dataclass
class RenderFrame:
    """Minimal render frame for the phase-1 event bridge."""

    transcript_entries: List[Dict[str, Any]] = field(default_factory=list)
    tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    task_activity_entries: List[Dict[str, Any]] = field(default_factory=list)
    input_enabled: bool = True
    input_mode: str = "normal"
    input_placeholder: str = ""
    active_modal: str = "none"
    modal_state: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    permission_audit: List[Dict[str, Any]] = field(default_factory=list)
    session_cost_text: str = ""
    session_cost_value: float = 0.0


class RendererBackend(Protocol):
    """Renderer backend abstraction used by UiRuntime."""

    def mount(self, initial_frame: RenderFrame) -> None: ...

    def update(self, frame: RenderFrame) -> None: ...

    def show_modal(self, modal_state: Dict[str, Any]) -> None: ...

    def clear_modal(self) -> None: ...

    def shutdown(self, final_snapshot: RenderFrame) -> None: ...

    async def ask_permission(self, tc, dangerous: bool = False) -> str: ...

    async def pick_from_list(
        self, title: str, values: list[tuple[str, str]],
    ) -> str | None: ...


@runtime_checkable
class RendererBackendWithPrepare(Protocol):
    """Optional runtime-preparation hook for backends."""

    async def prepare_runtime(self) -> None: ...


@runtime_checkable
class RendererBackendWithEventRender(Protocol):
    """Optional legacy event-rendering hook for non-frame transcript backends."""

    def render_event(self, event: StreamEvent) -> None: ...


class TaskEventSink:
    """Narrow sink injected into AgentTaskRuntime for UI snapshots."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TaskUiEvent] = asyncio.Queue()

    def emit(self, event: TaskUiEvent) -> None:
        self._queue.put_nowait(event)

    async def get(self) -> TaskUiEvent:
        return await self._queue.get()


class UiRuntime:
    """Bridges engine stream events and task runtime events into one UI flow."""

    FRAME_INTERVAL_SECONDS = 1.0 / 30.0
    MAX_TASK_ACTIVITY_ENTRIES = 50

    def __init__(self, *, engine: Any, ui: RendererBackend):
        self.engine = engine
        self.ui = ui
        self.frame = RenderFrame()
        self.task_sink = TaskEventSink()
        self._dirty = False
        self._last_flush_at = 0.0
        self._mounted = False

    def _task_runtime(self) -> Any | None:
        core_engine = getattr(self.engine, "core_engine", None)
        return getattr(core_engine, "task_runtime", None)

    def _to_ui_event(self, event: StreamEvent) -> UiEvent:
        mapping = {
            "text": "assistant_delta",
            "done": "assistant_done",
            "thinking": "thinking_delta",
            "tool_call": "tool_call_started",
            "tool_result": "tool_call_finished",
            "permission_needed": "permission_requested",
            "error": "ui_fatal",
            "cost": "session_cost_updated",
        }
        return UiEvent(
            type=mapping.get(event.type, event.type),
            content=event.content,
            metadata=dict(event.metadata),
        )

    async def run_submit_message(
        self,
        *,
        user_input: str,
        state_to_pass: Any,
        session_id: str,
    ) -> list[StreamEvent]:
        """Consume one engine submission and drive the legacy UI/backend."""

        task_runtime = self._task_runtime()
        if task_runtime is not None:
            task_runtime.set_task_event_sink(self.task_sink)
        events: List[StreamEvent] = []
        self.frame.input_enabled = False
        self.frame.input_mode = "normal"
        self.frame.input_placeholder = "Agent is responding..."
        await self._prepare_backend()
        if not self._mounted:
            self.ui.mount(self.frame)
            self._mounted = True
        self._mark_dirty()
        submit_iter = self.engine.submit_message(
            user_input,
            state_to_pass,
            session_id=session_id,
        )
        try:
            async for event in submit_iter:
                ui_event = self._to_ui_event(event)
                if ui_event.type == "permission_requested":
                    await self._handle_permission_event(ui_event)
                else:
                    self._apply_ui_event(ui_event)
                    self._maybe_render_event(event)
                    await self._flush_if_due()
                await self._drain_task_events()
                events.append(event)
            return events
        finally:
            if task_runtime is not None:
                task_runtime.set_task_event_sink(None)
            if self._dirty:
                await self._flush(force=True)

    async def shutdown(self) -> None:
        if self._dirty:
            await self._flush(force=True)
        self.ui.shutdown(self.frame)

    async def _prepare_backend(self) -> None:
        if not isinstance(self.ui, RendererBackendWithPrepare):
            return
        await self.ui.prepare_runtime()

    async def _handle_permission_event(self, event: UiEvent) -> None:
        self.frame.active_modal = "permission_request"
        self.frame.input_enabled = True
        self.frame.input_mode = "permission_modal"
        self.frame.input_placeholder = "Answer with y/n/a/d and press Enter."
        self.frame.modal_state = self._build_permission_modal_state(event)
        self._mark_dirty()
        self.ui.show_modal(self.frame.modal_state)
        tc = event.metadata.get("tool_call")
        skill_shell_request = event.metadata.get("skill_shell_request")
        mcp_trust_request = event.metadata.get("mcp_trust_request")
        answer = "deny"
        if skill_shell_request is not None:
            class _SkillShellPrompt:
                name = f"skill-shell:{skill_shell_request.skill_name}"
                input = {"command": skill_shell_request.command, "file_path": ""}

            answer = await self.ui.ask_permission(_SkillShellPrompt(), dangerous=True)
            self.engine.resolve_skill_permission(answer in ("once", "always"))
        elif mcp_trust_request is not None:
            class _McpTrustPrompt:
                name = "mcp-project-trust"
                input = {
                    "command": self._format_mcp_trust_target(mcp_trust_request),
                    "file_path": "",
                }

            answer = await self.ui.ask_permission(_McpTrustPrompt(), dangerous=True)
            self.engine.resolve_mcp_trust(answer in ("once", "always"))
        elif tc is not None:
            answer = await self.ui.ask_permission(
                tc,
                dangerous=event.metadata.get(
                    "dangerous",
                    event.metadata.get("risk") == "high",
                ),
            )
            if answer in ("once", "always"):
                decision = "always" if answer == "always" else "once"
                self.engine.resolve_permission(decision, tc.name)
            else:
                self.engine.resolve_permission("deny", "")
        self._record_permission_audit(event, answer)
        self.frame.active_modal = "none"
        self.frame.input_enabled = False
        self.frame.input_mode = "normal"
        self.frame.input_placeholder = "Agent is responding..."
        self.frame.modal_state = {}
        self._mark_dirty()
        self.ui.clear_modal()
        await self._flush(force=True)

    def _build_permission_modal_state(self, event: UiEvent) -> Dict[str, Any]:
        from ..cli.ui_shared import (
            PHASE1_PERMISSION_ACTION_LABELS,
            translate_backend_risk_level,
        )

        tc = event.metadata.get("tool_call")
        skill_shell_request = event.metadata.get("skill_shell_request")
        mcp_trust_request = event.metadata.get("mcp_trust_request")
        backend_risk_level = event.metadata.get("risk") or (
            "high" if event.metadata.get("dangerous", False) else "normal"
        )
        display_risk_level = translate_backend_risk_level(backend_risk_level)

        if tc is not None:
            tool_name = getattr(tc, "name", "")
            tool_input = getattr(tc, "input", {}) or {}
            target_summary = (
                tool_input.get("file_path")
                or tool_input.get("command")
                or tool_input.get("pattern")
                or ""
            )
            summary_lines = []
            if tool_name == "run_shell":
                command = str(tool_input.get("command", ""))
                summary_lines = [command[:200]]
            elif tool_name in ("write_file", "edit_file"):
                summary_lines = [str(tool_input.get("content", ""))[:200]]
            kind = "tool_permission"
        elif skill_shell_request is not None:
            tool_name = "skill-shell"
            target_summary = getattr(skill_shell_request, "command", "")
            summary_lines = [target_summary[:200]]
            kind = "skill_shell_permission"
        elif mcp_trust_request is not None:
            tool_name = "mcp-project-trust"
            target_summary = self._format_mcp_trust_target(mcp_trust_request)
            summary_lines = [target_summary[:200]]
            kind = "mcp_trust_permission"
        else:
            tool_name = ""
            target_summary = ""
            summary_lines = []
            kind = "permission_request"

        return {
            "kind": kind,
            "tool_name": tool_name,
            "target_summary": str(target_summary),
            "backend_risk_level": backend_risk_level,
            "display_risk_level": display_risk_level,
            "dangerous": bool(event.metadata.get("dangerous", False)),
            "summary_lines": [line for line in summary_lines if line],
            "action_labels": list(PHASE1_PERMISSION_ACTION_LABELS),
        }

    @staticmethod
    def _format_mcp_trust_target(request: Any) -> str:
        if not isinstance(request, list):
            return ""
        parts: list[str] = []
        for item in request:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            command = item.get("command") or item.get("url") or ""
            args = item.get("args") or []
            arg_text = " ".join(str(arg) for arg in args)
            parts.append(f"{name}: {command} {arg_text}".strip())
        return "; ".join(parts)

    def _record_permission_audit(self, event: UiEvent, answer: str) -> None:
        audit_entry = {
            "tool_name": self.frame.modal_state.get("tool_name", ""),
            "risk_level": self.frame.modal_state.get("backend_risk_level", "low"),
            "dangerous": self.frame.modal_state.get("dangerous", False),
            "target_summary": self.frame.modal_state.get("target_summary", ""),
            "decision": answer,
        }
        self.frame.permission_audit.append(audit_entry)

    def _apply_ui_event(self, event: UiEvent) -> None:
        if event.type == "assistant_delta":
            self._append_transcript_entry("assistant", event.content)
            self._mark_dirty()
        elif event.type == "thinking_delta":
            self._append_transcript_entry("thinking", event.content)
            self._mark_dirty()
        elif event.type == "tool_call_started":
            self._append_transcript_entry("tool_call", self._format_tool_call_entry(event))
            self._mark_dirty()
        elif event.type == "tool_call_finished":
            self._append_transcript_entry("tool_result", self._format_tool_result_entry(event))
            self._mark_dirty()
        elif event.type == "session_cost_updated":
            self.frame.session_cost_text = event.content
            self.frame.session_cost_value = float(event.metadata.get("cost", 0) or 0.0)
            self._mark_dirty()
        elif event.type == "ui_warning":
            if event.content:
                self.frame.warnings.append(event.content)
                self._append_transcript_entry("warning", event.content)
                self._mark_dirty()
        elif event.type == "ui_fatal":
            if event.content:
                self.frame.errors.append(event.content)
                self._append_transcript_entry("error", event.content)
                self._mark_dirty()

    async def _drain_task_events(self) -> None:
        updated = False
        while True:
            try:
                task_event = self.task_sink._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.frame.tasks[task_event.task_id] = task_event.record
            if task_event.type == "task_summary_available":
                self._record_task_activity(task_event)
            updated = True
        if updated:
            self._mark_dirty()
            await self._flush_if_due()

    def _mark_dirty(self) -> None:
        self._dirty = True

    async def _flush_if_due(self) -> None:
        if not self._dirty:
            return
        now = time.monotonic()
        if self._last_flush_at <= 0.0 or (
            now - self._last_flush_at >= self.FRAME_INTERVAL_SECONDS
        ):
            await self._flush(force=True)

    async def _flush(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        self.ui.update(self.frame)
        self._dirty = False
        self._last_flush_at = time.monotonic()

    def _maybe_render_event(self, event: StreamEvent) -> None:
        if getattr(self.ui, "uses_frame_transcript", False):
            return
        if not isinstance(self.ui, RendererBackendWithEventRender):
            return
        self.ui.render_event(event)

    def _append_transcript_entry(self, kind: str, text: str) -> None:
        if not text:
            return
        entries = self.frame.transcript_entries
        if entries and entries[-1].get("kind") == kind and kind in ("assistant", "thinking"):
            entries[-1]["text"] = entries[-1].get("text", "") + text
            return
        entries.append({"kind": kind, "text": text})

    def _record_task_activity(self, task_event: TaskUiEvent) -> None:
        record = task_event.record
        activity_entry = {
            "task_id": task_event.task_id,
            "worker_label": record.get("worker_label") or task_event.task_id,
            "status": record.get("status", "unknown"),
            "summary": task_event.summary,
            "result_text": task_event.result_text,
            "input_tokens": record.get("input_tokens", 0),
            "output_tokens": record.get("output_tokens", 0),
            "tool_use_count": record.get("tool_use_count", 0),
            "duration_ms": record.get("duration_ms", 0),
        }
        entries = self.frame.task_activity_entries
        if entries and entries[-1] == activity_entry:
            return
        entries.append(activity_entry)
        if len(entries) > self.MAX_TASK_ACTIVITY_ENTRIES:
            del entries[: len(entries) - self.MAX_TASK_ACTIVITY_ENTRIES]

    @staticmethod
    def _format_tool_call_entry(event: UiEvent) -> str:
        tool_name = event.content or event.metadata.get("tool_name") or "tool"
        return "[tool] {}".format(tool_name)

    @staticmethod
    def _format_tool_result_entry(event: UiEvent) -> str:
        metadata = event.metadata or {}
        if metadata.get("denied"):
            return "[tool denied]"
        result = metadata.get("result", event.content)
        if not result:
            return "[tool result]"
        preview = str(result).strip().splitlines()[0][:200]
        return "[tool result] {}".format(preview)
