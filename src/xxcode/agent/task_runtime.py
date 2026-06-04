"""Runtime support for multi-agent task orchestration."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..security.permission import PermissionState
from ..tools.registry import ToolRegistry
from .definitions import AgentDef
from .state import AgentState
from .subagent import SubAgent, SubAgentSessionState

logger = logging.getLogger(__name__)

HARD_MAX_WAIT_SECONDS = 3600
IDLE_TTL_SECONDS = 900
TERMINAL_RECORD_TTL_SECONDS = 900

TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "killed", "interrupted"})
REUSABLE_STABLE_STATUSES = frozenset({"idle", "failed", "killed", "interrupted"})
NON_REUSABLE_STABLE_STATUSES = frozenset({"completed", "failed", "killed", "interrupted"})


@dataclass
class AgentTaskRecord:
    """Persistent task metadata."""

    task_id: str
    parent_task_id: str | None
    parent_scope_id: str
    worker_label: str
    description: str
    agent_type: str
    reusable: bool
    status: str
    created_at: float
    updated_at: float
    result_text: str = ""
    error_text: str = ""
    result_file: str = ""
    error_file: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_use_count: int = 0
    duration_ms: int = 0
    termination_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTaskRecord":
        return cls(
            task_id=str(data.get("task_id", "")),
            parent_task_id=data.get("parent_task_id"),
            parent_scope_id=str(data.get("parent_scope_id", "main")),
            worker_label=str(data.get("worker_label", "")),
            description=str(data.get("description", "")),
            agent_type=str(data.get("agent_type", "general-purpose")),
            reusable=bool(data.get("reusable", False)),
            status=str(data.get("status", "interrupted")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            result_text=str(data.get("result_text", "")),
            error_text=str(data.get("error_text", "")),
            result_file=str(data.get("result_file", "")),
            error_file=str(data.get("error_file", "")),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            tool_use_count=int(data.get("tool_use_count", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            termination_reason=str(data.get("termination_reason", "")),
        )


@dataclass
class TaskNotification:
    """Scope-local notification for a task state change."""

    notification_id: str
    task_id: str
    parent_task_id: str | None
    parent_scope_id: str
    status: str
    summary: str
    result: str
    usage: dict[str, int] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        text = (
            "<task-notification>\n"
            f"notification_id: {self.notification_id}\n"
            f"task_id: {self.task_id}\n"
            f"parent_task_id: {self.parent_task_id or ''}\n"
            f"parent_scope_id: {self.parent_scope_id}\n"
            f"status: {self.status}\n"
            f"summary: {self.summary}\n"
            f"result: {self.result}\n"
            f"usage: {json.dumps(self.usage, ensure_ascii=False)}\n"
            "</task-notification>"
        )
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "isMeta": True,
            "metadata": {
                "source": "task_notification",
                "task_id": self.task_id,
                "notification_id": self.notification_id,
                "parent_task_id": self.parent_task_id,
                "parent_scope_id": self.parent_scope_id,
                "status": self.status,
            },
        }


@dataclass
class TaskWaiter:
    """Registered waiter for one TaskWait call."""

    task_ids: set[str]
    future: asyncio.Future[None]


@dataclass
class ScopeRuntime:
    """Per-scope notification queue and waiter registry."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_notifications: list[TaskNotification] = field(default_factory=list)
    waiters: dict[str, TaskWaiter] = field(default_factory=dict)


@dataclass
class ScopeCleanupReport:
    """Summary of cleaning up an ephemeral scope."""

    active_tasks_stopped: int = 0
    tasks_removed: int = 0


@dataclass
class WorkerSession:
    """Live worker session state."""

    runtime: "AgentTaskRuntime"
    record: AgentTaskRecord
    subagent: SubAgent
    session_state: SubAgentSessionState
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    abort_requested: bool = False
    _current_request_task: asyncio.Task[Any] | None = None
    _idle_ttl_task: asyncio.Task[Any] | None = None
    idle_epoch: int = 0
    loop_task: asyncio.Task[Any] | None = None
    _first_request_pending: bool = True
    worktree_path: Path | None = None

    def start(self) -> None:
        self.loop_task = asyncio.create_task(
            self._run_loop(),
            name=f"worker-session-{self.record.task_id}",
        )

    async def _run_loop(self) -> None:
        active_request_started_at: float | None = None
        try:
            while True:
                message = await self.queue.get()
                async with self.lock:
                    status = self.record.status
                    if status in TERMINAL_TASK_STATUSES:
                        return
                    if not isinstance(message, dict) or not isinstance(message.get("prompt"), str):
                        self.runtime._fail_task_consistency(
                            self.record.task_id,
                            "Invalid worker queue payload. Expected {'prompt': <str>}.",
                        )
                        return
                    if status != "queued":
                        self.runtime._fail_task_consistency(
                            self.record.task_id,
                            f"Inconsistent worker state after queue wakeup: {status}",
                        )
                        return
                    self.abort_requested = False
                    self.runtime._set_record_status(self.record, "running")
                    is_first_request = self._first_request_pending
                    if not is_first_request:
                        self.session_state.messages.append({
                            "role": "user",
                            "content": [{"type": "text", "text": message["prompt"]}],
                        })
                    self._current_request_task = asyncio.create_task(
                        self.subagent._execute_one_request(message["prompt"], self.session_state),
                        name=f"worker-request-{self.record.task_id}",
                    )

                started_at = time.monotonic()
                active_request_started_at = started_at
                start_input_tokens = self.session_state.total_input_tokens
                start_output_tokens = self.session_state.total_output_tokens
                start_tool_use_count = self.session_state.total_tool_use_count
                try:
                    result = await self._current_request_task
                except asyncio.CancelledError:
                    await self._handle_loop_cancellation(started_at)
                    active_request_started_at = None
                    return
                except Exception as exc:
                    logger.exception("Worker session %s crashed", self.record.task_id)
                    async with self.lock:
                        self._current_request_task = None
                        self._cancel_idle_ttl_locked()
                        self.runtime._set_record_status(self.record, "failed")
                        self.record.termination_reason = "failed"
                        self.record.error_text = str(exc)
                        self.record.duration_ms += int((time.monotonic() - started_at) * 1000)
                        self.runtime._enqueue_notification_for_record(
                            self.record,
                            summary="Worker failed.",
                            result_text=self.record.error_text,
                        )
                    active_request_started_at = None
                    return

                duration_ms = int((time.monotonic() - started_at) * 1000)
                active_request_started_at = None
                input_delta = self.session_state.total_input_tokens - start_input_tokens
                output_delta = self.session_state.total_output_tokens - start_output_tokens
                tool_use_delta = self.session_state.total_tool_use_count - start_tool_use_count
                async with self.lock:
                    self._current_request_task = None
                    self.record.input_tokens += input_delta
                    self.record.output_tokens += output_delta
                    self.record.tool_use_count += tool_use_delta
                    self.record.duration_ms += duration_ms
                    self.record.result_text = result.final_text
                    self.record.error_text = ""
                    self.record.termination_reason = "completed"
                    if self.record.status == "killed":
                        return
                    self._first_request_pending = False
                    if self.record.reusable:
                        self.idle_epoch += 1
                        self.runtime._set_record_status(self.record, "idle")
                        self._start_idle_ttl_locked(self.idle_epoch)
                    else:
                        self.runtime._set_record_status(self.record, "completed")
                    self.runtime._enqueue_notification_for_record(
                        self.record,
                        summary="Worker completed request.",
                        result_text=result.final_text,
                    )
                    if not self.record.reusable:
                        return
        except asyncio.CancelledError:
            await self._handle_loop_cancellation(active_request_started_at)
            return
        finally:
            await self.runtime._finalize_worker_session(self)

    async def _cancel_current_request_task(self) -> None:
        current_request_task: asyncio.Task[Any] | None = None
        async with self.lock:
            current_request_task = self._current_request_task
            self._current_request_task = None
        if current_request_task is None:
            return
        if not current_request_task.done():
            current_request_task.cancel()
        try:
            await current_request_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "Worker request %s finished with an exception during cancellation cleanup.",
                self.record.task_id,
                exc_info=True,
            )

    async def _handle_loop_cancellation(self, active_request_started_at: float | None) -> None:
        await self._cancel_current_request_task()
        async with self.lock:
            self.abort_requested = True
            self._cancel_idle_ttl_locked()
            if self.record.status in TERMINAL_TASK_STATUSES:
                return
            self.runtime._set_record_status(self.record, "interrupted")
            self.record.termination_reason = "cancelled"
            self.record.error_text = "Worker interrupted while processing the request."
            if active_request_started_at is not None:
                self.record.duration_ms += int((time.monotonic() - active_request_started_at) * 1000)
            self.runtime._enqueue_notification_for_record(
                self.record,
                summary="Worker interrupted.",
                result_text=self.record.error_text,
            )

    def _cancel_idle_ttl_locked(self) -> None:
        if self._idle_ttl_task is not None and not self._idle_ttl_task.done():
            self._idle_ttl_task.cancel()
        self._idle_ttl_task = None

    def _start_idle_ttl_locked(self, epoch: int) -> None:
        self._cancel_idle_ttl_locked()
        self._idle_ttl_task = asyncio.create_task(
            self._idle_ttl_loop(epoch),
            name=f"worker-idle-ttl-{self.record.task_id}",
        )

    async def _idle_ttl_loop(self, epoch: int) -> None:
        loop_task_to_cancel: asyncio.Task[Any] | None = None
        try:
            await asyncio.sleep(IDLE_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        async with self.lock:
            if (
                self.record.status != "idle"
                or self.idle_epoch != epoch
            ):
                return
            self.runtime._set_record_status(self.record, "completed")
            self.record.termination_reason = "idle_ttl_expired"
            self.runtime._enqueue_notification_for_record(
                self.record,
                summary="Reusable worker expired after idling.",
                result_text=self.record.result_text,
            )
            loop_task_to_cancel = self.loop_task
        if loop_task_to_cancel is not None and not loop_task_to_cancel.done():
            loop_task_to_cancel.cancel()


class AgentTaskRuntime:
    """In-memory runtime for background sub-agent tasks."""

    def __init__(self) -> None:
        self._records: dict[str, AgentTaskRecord] = {}
        self._workers: dict[str, WorkerSession] = {}
        self._scopes: dict[str, ScopeRuntime] = {"main": ScopeRuntime()}
        self._child_counts: dict[str, int] = {}
        self._task_parent_scopes: dict[str, str] = {}
        self._expired_task_parent_scopes: dict[str, str] = {}
        self._record_cleanup_tasks: dict[str, asyncio.Task[Any]] = {}
        self._scope_cleanup_in_progress: set[str] = set()
        self._task_event_sink: Any | None = None

    def set_task_event_sink(self, sink: Any | None) -> None:
        self._task_event_sink = sink

    def export_snapshot(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._records.values()]

    def import_snapshot(self, snapshot: list[dict[str, Any]] | None) -> None:
        self._cancel_all_record_cleanup_tasks()
        self._records.clear()
        self._workers.clear()
        self._scopes = {"main": ScopeRuntime()}
        self._child_counts.clear()
        self._task_parent_scopes.clear()
        self._expired_task_parent_scopes.clear()
        self._scope_cleanup_in_progress.clear()
        if not snapshot:
            return
        for raw in snapshot:
            record = AgentTaskRecord.from_dict(raw)
            resumed_live_task = record.status in {"queued", "running", "idle"}
            if resumed_live_task:
                record.status = "interrupted"
                record.updated_at = time.time()
                if not record.termination_reason:
                    record.termination_reason = "session_resumed"
            self._register_record(record, create_task_scope=False)
            if resumed_live_task:
                self._enqueue_notification_for_record(
                    record,
                    summary="Worker was interrupted when the session resumed.",
                    result_text=record.error_text or record.result_text,
                )

    async def shutdown(self) -> None:
        worker_ids = list(self._workers)
        for task_id in worker_ids:
            await self.stop_task(task_id, scope_id=self._records[task_id].parent_scope_id, force_scope_check=False)
        for worker in list(self._workers.values()):
            if worker.loop_task is not None and not worker.loop_task.done():
                worker.loop_task.cancel()
                try:
                    await worker.loop_task
                except asyncio.CancelledError:
                    pass
        self._cancel_all_record_cleanup_tasks()

    async def cleanup_scope(
        self,
        scope_id: str,
    ) -> ScopeCleanupReport:
        """Stop and discard all descendants of an ephemeral scope."""
        report = ScopeCleanupReport()
        while True:
            direct_children = [
                record
                for record in list(self._records.values())
                if record.parent_scope_id == scope_id
            ]
            if not direct_children:
                break

            for record in direct_children:
                if record.status not in TERMINAL_TASK_STATUSES:
                    report.active_tasks_stopped += 1
                    await self.stop_task(
                        record.task_id,
                        scope_id=record.parent_scope_id,
                        force_scope_check=False,
                    )

                child_report = await self.cleanup_scope(record.task_id)
                report.active_tasks_stopped += child_report.active_tasks_stopped
                report.tasks_removed += child_report.tasks_removed

                worker = self._workers.get(record.task_id)
                if worker is not None:
                    self._scope_cleanup_in_progress.add(record.task_id)
                    try:
                        async with worker.lock:
                            worker._cancel_idle_ttl_locked()
                            current_request_task = worker._current_request_task
                            worker._current_request_task = None
                        if current_request_task is not None and not current_request_task.done():
                            current_request_task.cancel()
                            try:
                                await current_request_task
                            except asyncio.CancelledError:
                                pass
                        if worker.loop_task is not None and not worker.loop_task.done():
                            worker.loop_task.cancel()
                            try:
                                await worker.loop_task
                            except asyncio.CancelledError:
                                pass
                    finally:
                        self._scope_cleanup_in_progress.discard(record.task_id)

                    # Belt-and-suspenders worktree cleanup for scope teardown.
                    if worker.worktree_path is not None:
                        try:
                            from .worktree import WorktreeManager
                            await WorktreeManager.remove(worker.worktree_path)
                        except Exception:
                            logger.debug(
                                "Worktree cleanup in scope teardown failed for %s",
                                worker.worktree_path,
                                exc_info=True,
                            )

                self._workers.pop(record.task_id, None)
                removed = self._unregister_record(record.task_id)
                if removed is not None:
                    self._cancel_record_cleanup(record.task_id)
                    self._forget_task_identity(record.task_id)
                    report.tasks_removed += 1

        scope = self._scopes.pop(scope_id, None)
        if scope is not None:
            async with scope.lock:
                scope.pending_notifications.clear()
                for waiter in scope.waiters.values():
                    if waiter.future.done():
                        continue
                    waiter.future.cancel()
                scope.waiters.clear()
        return report

    def ensure_scope(self, scope_id: str) -> ScopeRuntime:
        if scope_id not in self._scopes:
            self._scopes[scope_id] = ScopeRuntime()
        return self._scopes[scope_id]

    def list_tasks(self, scope_id: str) -> list[AgentTaskRecord]:
        return sorted(
            [
                record
                for record in self._records.values()
                if record.parent_scope_id == scope_id
            ],
            key=lambda record: record.created_at,
        )

    def register_foreground_task(
        self,
        *,
        task_id: str,
        parent_task_id: str | None,
        parent_scope_id: str,
        worker_label: str,
        description: str,
        agent_type: str,
    ) -> AgentTaskRecord:
        now = time.time()
        record = AgentTaskRecord(
            task_id=task_id,
            parent_task_id=parent_task_id,
            parent_scope_id=parent_scope_id,
            worker_label=worker_label,
            description=description,
            agent_type=agent_type,
            reusable=False,
            status="running",
            created_at=now,
            updated_at=now,
        )
        self._register_record(record, create_task_scope=False)
        return record

    def complete_foreground_task(
        self,
        record: AgentTaskRecord,
        *,
        termination_reason: str,
    ) -> None:
        self._set_record_status(record, "completed")
        record.termination_reason = termination_reason

    def fail_foreground_task(
        self,
        record: AgentTaskRecord,
        *,
        termination_reason: str,
    ) -> None:
        self._set_record_status(record, "failed")
        record.termination_reason = termination_reason

    def discard_foreground_task(self, task_id: str) -> None:
        self._unregister_record(task_id)
        self._forget_task_identity(task_id)

    def get_task(self, task_id: str, scope_id: str, *, force_scope_check: bool = True) -> AgentTaskRecord | None:
        record = self._records.get(task_id)
        if record is None:
            return None
        if force_scope_check and record.parent_scope_id != scope_id:
            return None
        return record

    async def spawn_worker(
        self,
        *,
        config: Any,
        registry: ToolRegistry,
        definition: AgentDef,
        parent_state: AgentState | None,
        description: str,
        prompt: str,
        agent_type: str,
        model_override: str | None,
        worker_label: str,
        reusable: bool,
        parent_scope_id: str,
        parent_task_id: str | None,
        extra_context: dict[str, Any] | None = None,
        worktree_path: str | None = None,
    ) -> AgentTaskRecord:
        task_id = f"subagent-{agent_type}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        record = AgentTaskRecord(
            task_id=task_id,
            parent_task_id=parent_task_id,
            parent_scope_id=parent_scope_id,
            worker_label=worker_label,
            description=description,
            agent_type=agent_type,
            reusable=reusable,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._register_record(record, create_task_scope=True)

        worker_state = build_subagent_state(parent_state, definition.permission_mode)
        worker_extra_context = {
            **(extra_context or {}),
            "allowed_read_roots": [str(config.cwd)],
            "task_runtime": self,
            "scope_id": task_id,
            "current_task_id": task_id,
            "parent_task_id": parent_task_id,
            "parent_scope_id": parent_scope_id,
            "current_agent_type": agent_type,
            "_drain_pending_notifications": self.drain_pending_notifications,
        }
        subagent = SubAgent(
            config=config,
            registry=registry,
            definition=definition,
            parent_state=worker_state,
            model_override=model_override,
            agent_type=agent_type,
            extra_context=worker_extra_context,
        )
        session_state = await subagent._create_session_state(prompt)
        worker = WorkerSession(
            runtime=self,
            record=record,
            subagent=subagent,
            session_state=session_state,
            worktree_path=Path(worktree_path) if worktree_path else None,
        )
        session_state.abort_check = lambda worker=worker: worker.abort_requested
        self._workers[task_id] = worker
        worker.start()
        await worker.queue.put({"prompt": prompt})
        return record

    async def send_message(self, task_id: str, scope_id: str, prompt: str) -> AgentTaskRecord | str:
        record = self.get_task(task_id, scope_id)
        worker = self._workers.get(task_id)
        if record is None or worker is None:
            return "Error: Worker is not reusable or has already terminated."

        async with worker.lock:
            if not record.reusable or record.status in TERMINAL_TASK_STATUSES:
                return "Error: Worker is not reusable or has already terminated."
            if record.status != "idle":
                return "Error: Worker is currently busy processing previous request."
            worker._cancel_idle_ttl_locked()
            self._set_record_status(record, "queued")
            try:
                worker.queue.put_nowait({"prompt": prompt})
            except asyncio.QueueFull:
                self._set_record_status(record, "idle")
                worker._start_idle_ttl_locked(worker.idle_epoch)
                return "Error: Worker is currently busy processing previous request."
        return record

    async def stop_task(
        self,
        task_id: str,
        scope_id: str,
        *,
        force_scope_check: bool = True,
    ) -> AgentTaskRecord | str:
        record = self.get_task(task_id, scope_id, force_scope_check=force_scope_check)
        if record is None:
            return "Error: Task not found."
        worker = self._workers.get(task_id)
        if worker is None:
            if record.status not in TERMINAL_TASK_STATUSES:
                self._set_record_status(record, "killed")
                record.termination_reason = "stopped"
                self._enqueue_notification_for_record(
                    record,
                    summary="Worker was stopped after its live session disappeared.",
                    result_text=record.result_text or record.error_text,
                )
                self._schedule_terminal_record_cleanup(record.task_id)
            return record

        loop_task_to_await: asyncio.Task[Any] | None = None
        async with worker.lock:
            if record.status == "queued":
                worker._cancel_idle_ttl_locked()
                self._set_record_status(record, "killed")
                record.termination_reason = "stopped"
                loop_task_to_await = worker.loop_task
                self._enqueue_notification_for_record(
                    record,
                    summary="Queued worker was stopped.",
                    result_text="",
                )
            elif record.status == "idle":
                worker._cancel_idle_ttl_locked()
                self._set_record_status(record, "killed")
                record.termination_reason = "stopped"
                loop_task_to_await = worker.loop_task
                self._enqueue_notification_for_record(
                    record,
                    summary="Idle worker was stopped.",
                    result_text=record.result_text,
                )
            elif record.status == "running":
                worker.abort_requested = True
                self._set_record_status(record, "killed")
                record.termination_reason = "stopped"
                if worker._current_request_task is not None and not worker._current_request_task.done():
                    worker._current_request_task.cancel()
                self._enqueue_notification_for_record(
                    record,
                    summary="Running worker was stopped.",
                    result_text=record.result_text or record.error_text,
                )
            elif record.status in TERMINAL_TASK_STATUSES:
                loop_task_to_await = worker.loop_task
        if loop_task_to_await is not None and not loop_task_to_await.done():
            loop_task_to_await.cancel()
            try:
                await loop_task_to_await
            except asyncio.CancelledError:
                pass
        return record

    async def wait_for_tasks(
        self,
        task_ids: list[str],
        scope_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not task_ids:
            return {"tasks": [], "timeout": False, "pending_task_ids": []}

        loop = asyncio.get_running_loop()
        soft_deadline = loop.time() + timeout_seconds
        hard_deadline = loop.time() + HARD_MAX_WAIT_SECONDS

        scope = self.ensure_scope(scope_id)
        wanted = set(task_ids)

        while True:
            waiter_id: str | None = None
            waiter_future: asyncio.Future[None] | None = None
            now = loop.time()
            async with scope.lock:
                self._consume_matching_notifications_unlocked(scope, wanted)
                snapshots = self._snapshot_tasks_for_scope(task_ids, scope_id)
                inaccessible_task_ids = self._find_inaccessible_task_ids(task_ids, scope_id)
                expired_task_ids = self._find_expired_task_ids(task_ids, scope_id)
                if inaccessible_task_ids:
                    return {
                        "error": (
                            "Error: Unknown or inaccessible task ids: "
                            f"{', '.join(inaccessible_task_ids)}."
                        ),
                        **snapshots,
                        "inaccessible_task_ids": inaccessible_task_ids,
                        "expired_task_ids": expired_task_ids,
                    }
                if expired_task_ids:
                    return {
                        "error": (
                            "Error: Task records expired or were already cleaned up: "
                            f"{', '.join(expired_task_ids)}."
                        ),
                        **snapshots,
                        "inaccessible_task_ids": [],
                        "expired_task_ids": expired_task_ids,
                    }
                missing_unknown_task_ids = [
                    task_id
                    for task_id in snapshots["missing_task_ids"]
                    if task_id not in expired_task_ids
                ]
                if missing_unknown_task_ids:
                    return {
                        "error": (
                            "Error: Unknown or inaccessible task ids: "
                            f"{', '.join(missing_unknown_task_ids)}."
                        ),
                        **snapshots,
                        "inaccessible_task_ids": missing_unknown_task_ids,
                        "expired_task_ids": [],
                    }
                snapshots["expired_task_ids"] = expired_task_ids
                snapshots["inaccessible_task_ids"] = []
                all_stable = all(
                    self._is_stable_record(self._records[task_id])
                    for task_id in task_ids
                    if task_id in self._records
                )
                if all_stable or now >= soft_deadline or now >= hard_deadline:
                    if now >= hard_deadline:
                        return {
                            "error": (
                                "Error: TaskWait timed out waiting for workers after "
                                f"{HARD_MAX_WAIT_SECONDS} seconds."
                            ),
                            **snapshots,
                        }
                    snapshots["timeout"] = now >= soft_deadline and not all_stable
                    snapshots["pending_task_ids"] = [
                        task_id
                        for task_id in task_ids
                        if task_id in self._records and not self._is_stable_record(self._records[task_id])
                    ]
                    return snapshots

                waiter_id = uuid.uuid4().hex
                waiter_future = loop.create_future()
                scope.waiters[waiter_id] = TaskWaiter(task_ids=set(task_ids), future=waiter_future)

            try:
                max_wait = min(
                    max(soft_deadline - now, 0.0),
                    max(hard_deadline - now, 0.0),
                )
                await asyncio.wait_for(asyncio.shield(waiter_future), timeout=max_wait)
            except asyncio.TimeoutError:
                pass
            finally:
                async with scope.lock:
                    if waiter_id is not None:
                        scope.waiters.pop(waiter_id, None)
                    self._consume_matching_notifications_unlocked(scope, wanted)

    async def drain_pending_notifications(
        self,
        *,
        scope_id: str,
        current_task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope = self.ensure_scope(scope_id)
        async with scope.lock:
            drained = [notification.to_message() for notification in scope.pending_notifications]
            scope.pending_notifications.clear()
            return drained

    def _snapshot_tasks_for_scope(
        self,
        task_ids: list[str],
        scope_id: str,
    ) -> dict[str, Any]:
        missing: list[str] = []
        tasks: list[dict[str, Any]] = []
        for task_id in task_ids:
            record = self.get_task(task_id, scope_id)
            if record is None:
                missing.append(task_id)
                continue
            tasks.append(record.to_dict())
        return {
            "tasks": tasks,
            "timeout": False,
            "pending_task_ids": [],
            "missing_task_ids": missing,
            "expired_task_ids": [],
            "inaccessible_task_ids": [],
        }

    def _find_inaccessible_task_ids(
        self,
        task_ids: list[str],
        scope_id: str,
    ) -> list[str]:
        inaccessible: list[str] = []
        for task_id in task_ids:
            if task_id in self._records:
                continue
            known_parent_scope = self._task_parent_scopes.get(task_id)
            expired_parent_scope = self._expired_task_parent_scopes.get(task_id)
            if known_parent_scope is not None and known_parent_scope != scope_id:
                inaccessible.append(task_id)
            elif expired_parent_scope is not None and expired_parent_scope != scope_id:
                inaccessible.append(task_id)
        return inaccessible

    def _find_expired_task_ids(
        self,
        task_ids: list[str],
        scope_id: str,
    ) -> list[str]:
        expired: list[str] = []
        for task_id in task_ids:
            if task_id in self._records:
                continue
            expired_parent_scope = self._expired_task_parent_scopes.get(task_id)
            if expired_parent_scope == scope_id:
                expired.append(task_id)
        return expired

    def _is_stable_record(self, record: AgentTaskRecord) -> bool:
        stable_statuses = REUSABLE_STABLE_STATUSES if record.reusable else NON_REUSABLE_STABLE_STATUSES
        return record.status in stable_statuses

    def _set_record_status(self, record: AgentTaskRecord, status: str) -> None:
        record.status = status
        record.updated_at = time.time()

    def _fail_task_consistency(self, task_id: str, error_text: str) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        self._set_record_status(record, "failed")
        record.error_text = error_text
        record.termination_reason = "internal_inconsistency"
        self._enqueue_notification_for_record(
            record,
            summary="Worker failed due to an internal runtime inconsistency.",
            result_text=error_text,
        )

    def _enqueue_notification_for_record(
        self,
        record: AgentTaskRecord,
        *,
        summary: str,
        result_text: str,
    ) -> None:
        notification = TaskNotification(
            notification_id=uuid.uuid4().hex[:12],
            task_id=record.task_id,
            parent_task_id=record.parent_task_id,
            parent_scope_id=record.parent_scope_id,
            status=record.status,
            summary=summary,
            result=result_text,
            usage={
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
            },
        )
        scope = self.ensure_scope(record.parent_scope_id)
        scope.pending_notifications.append(notification)
        for waiter in list(scope.waiters.values()):
            if record.task_id not in waiter.task_ids:
                continue
            if waiter.future.done():
                continue
            waiter.future.set_result(None)
        sink = self._task_event_sink
        if sink is not None:
            from ..ui.runtime import TaskUiEvent

            event_type = (
                "task_summary_available"
                if record.status in TERMINAL_TASK_STATUSES
                else "task_snapshot_updated"
            )
            sink.emit(
                TaskUiEvent(
                    type=event_type,
                    task_id=record.task_id,
                    record=record.to_dict(),
                    summary=summary,
                    result_text=result_text,
                )
            )

    def _consume_matching_notifications_unlocked(
        self,
        scope: ScopeRuntime,
        task_ids: set[str],
    ) -> list[TaskNotification]:
        matched = [
            notification
            for notification in scope.pending_notifications
            if notification.task_id in task_ids
        ]
        if matched:
            scope.pending_notifications = [
                notification
                for notification in scope.pending_notifications
                if notification.task_id not in task_ids
            ]
        return matched

    def _register_record(
        self,
        record: AgentTaskRecord,
        *,
        create_task_scope: bool,
    ) -> None:
        self._records[record.task_id] = record
        self._task_parent_scopes[record.task_id] = record.parent_scope_id
        self._child_counts[record.parent_scope_id] = (
            self._child_counts.get(record.parent_scope_id, 0) + 1
        )
        self.ensure_scope(record.parent_scope_id)
        if create_task_scope:
            self.ensure_scope(record.task_id)

    def _unregister_record(self, task_id: str) -> AgentTaskRecord | None:
        record = self._records.pop(task_id, None)
        if record is None:
            return None
        self._task_parent_scopes.pop(task_id, None)
        self._expired_task_parent_scopes[task_id] = record.parent_scope_id
        remaining = self._child_counts.get(record.parent_scope_id, 0) - 1
        if remaining > 0:
            self._child_counts[record.parent_scope_id] = remaining
        else:
            self._child_counts.pop(record.parent_scope_id, None)
        return record

    def _forget_task_identity(self, task_id: str) -> None:
        self._task_parent_scopes.pop(task_id, None)
        self._expired_task_parent_scopes.pop(task_id, None)

    async def _finalize_worker_session(self, worker: WorkerSession) -> None:
        async with worker.lock:
            worker._cancel_idle_ttl_locked()
            worker._current_request_task = None
            task_id = worker.record.task_id
            is_terminal = worker.record.status in TERMINAL_TASK_STATUSES

        current = self._workers.get(task_id)
        if current is worker:
            self._workers.pop(task_id, None)

        # Clean up the worktree if one was created for this worker.
        if worker.worktree_path is not None:
            try:
                from .worktree import WorktreeManager
                await WorktreeManager.remove(worker.worktree_path)
            except Exception:
                logger.debug(
                    "Worktree cleanup failed for %s", worker.worktree_path, exc_info=True,
                )

        if (
            is_terminal
            and task_id in self._records
            and task_id not in self._scope_cleanup_in_progress
        ):
            self._schedule_terminal_record_cleanup(task_id)

    def _cancel_all_record_cleanup_tasks(self) -> None:
        for task in self._record_cleanup_tasks.values():
            if task.done():
                continue
            task.cancel()
        self._record_cleanup_tasks.clear()

    def _cancel_record_cleanup(self, task_id: str) -> None:
        cleanup_task = self._record_cleanup_tasks.pop(task_id, None)
        if cleanup_task is None or cleanup_task.done():
            return
        cleanup_task.cancel()

    def _schedule_terminal_record_cleanup(self, task_id: str) -> None:
        record = self._records.get(task_id)
        if record is None or record.status not in TERMINAL_TASK_STATUSES:
            return
        existing = self._record_cleanup_tasks.get(task_id)
        if existing is not None and not existing.done():
            return
        self._record_cleanup_tasks[task_id] = asyncio.create_task(
            self._cleanup_terminal_record_after_ttl(task_id),
            name=f"task-record-cleanup-{task_id}",
        )

    async def _cleanup_terminal_record_after_ttl(self, task_id: str) -> None:
        try:
            await asyncio.sleep(TERMINAL_RECORD_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        removed = self._discard_terminal_record(task_id)
        if not removed:
            self._record_cleanup_tasks.pop(task_id, None)
            self._schedule_terminal_record_cleanup(task_id)

    def _discard_terminal_record(self, task_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None or record.status not in TERMINAL_TASK_STATUSES:
            self._record_cleanup_tasks.pop(task_id, None)
            self._forget_task_identity(task_id)
            return True
        if self._child_counts.get(task_id, 0) > 0:
            return False

        self._workers.pop(task_id, None)
        self._unregister_record(task_id)
        self._record_cleanup_tasks.pop(task_id, None)
        self._forget_task_identity(task_id)
        if task_id != "main":
            self._scopes.pop(task_id, None)
        return True


def clone_permission_state(state: PermissionState | None) -> PermissionState:
    if state is None:
        return PermissionState()
    return copy.deepcopy(state)


def build_subagent_state(
    parent_state: AgentState | None,
    permission_mode: str = "inherit",
) -> AgentState:
    """Build an isolated tool-facing AgentState for a worker."""
    child = AgentState()
    permission_state = clone_permission_state(
        getattr(parent_state, "permission_state", None)
    )
    if permission_mode == "bypass":
        permission_state.yolo_mode = True
    child.permission_state = permission_state
    return child
