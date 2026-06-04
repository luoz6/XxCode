"""Deeper tests for task runtime state transitions and waiting semantics."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import xxcode.agent.task_runtime as task_runtime_module

from xxcode.agent.subagent import SubAgentSessionState
from xxcode.agent.task_runtime import AgentTaskRecord, AgentTaskRuntime, WorkerSession
from xxcode.security.permission import PermissionState


def _record(
    *,
    task_id: str,
    parent_scope_id: str = "main",
    reusable: bool = False,
    status: str = "queued",
) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        parent_task_id=None,
        parent_scope_id=parent_scope_id,
        worker_label=task_id,
        description=task_id,
        agent_type="general-purpose",
        reusable=reusable,
        status=status,
        created_at=1.0,
        updated_at=1.0,
    )


def _register_task(
    runtime: AgentTaskRuntime,
    *,
    task_id: str,
    status: str,
    parent_scope_id: str = "main",
    reusable: bool = False,
) -> AgentTaskRecord:
    record = _record(
        task_id=task_id,
        parent_scope_id=parent_scope_id,
        reusable=reusable,
        status=status,
    )
    runtime._register_record(record, create_task_scope=True)
    return record


class _StubSubAgent:
    def __init__(self, responder=None):
        self._responder = responder
        self.calls: list[str] = []

    async def _execute_one_request(self, prompt: str, session_state: SubAgentSessionState):
        self.calls.append(prompt)
        if self._responder is None:
            return SimpleNamespace(final_text=f"done:{prompt}")
        return await self._responder(prompt, session_state)


def _worker(
    runtime: AgentTaskRuntime,
    *,
    task_id: str,
    status: str,
    reusable: bool = True,
    subagent: _StubSubAgent | None = None,
) -> tuple[AgentTaskRecord, WorkerSession, _StubSubAgent]:
    record = _record(task_id=task_id, status=status, reusable=reusable)
    stub = subagent or _StubSubAgent()
    worker = WorkerSession(
        runtime=runtime,
        record=record,
        subagent=stub,  # type: ignore[arg-type]
        session_state=SubAgentSessionState(),
    )
    runtime._register_record(record, create_task_scope=True)
    runtime._workers[task_id] = worker
    return record, worker, stub


def test_import_snapshot_converts_live_statuses_to_interrupted():
    runtime = AgentTaskRuntime()
    runtime.import_snapshot(
        [
            _record(task_id="queued", status="queued").to_dict(),
            _record(task_id="running", status="running").to_dict(),
            _record(task_id="idle", reusable=True, status="idle").to_dict(),
            _record(task_id="done", status="completed").to_dict(),
        ]
    )

    assert runtime.get_task("queued", "main").status == "interrupted"
    assert runtime.get_task("running", "main").status == "interrupted"
    assert runtime.get_task("idle", "main").status == "interrupted"
    assert runtime.get_task("done", "main").status == "completed"


def test_import_snapshot_enqueues_notifications_for_interrupted_workers():
    runtime = AgentTaskRuntime()
    runtime.import_snapshot(
        [
            _record(task_id="running", status="running").to_dict(),
            _record(task_id="done", status="completed").to_dict(),
        ]
    )

    drained = asyncio.run(runtime.drain_pending_notifications(scope_id="main"))

    assert len(drained) == 1
    assert drained[0]["metadata"]["task_id"] == "running"
    assert drained[0]["metadata"]["status"] == "interrupted"


def test_clone_permission_state_uses_deepcopy_instead_of_serialization(monkeypatch):
    state = PermissionState(
        confirmed_paths={"a"},
        confirmed_tools={"read_file"},
        yolo_mode=True,
    )

    def _boom(self):
        raise AssertionError("serialization path should not be used")

    monkeypatch.setattr(PermissionState, "to_dict", _boom)

    cloned = task_runtime_module.clone_permission_state(state)

    assert cloned is not state
    assert cloned.confirmed_paths == {"a"}
    assert cloned.confirmed_tools == {"read_file"}
    assert cloned.yolo_mode is True
    cloned.confirmed_paths.add("b")
    assert "b" not in state.confirmed_paths


def test_wait_for_tasks_returns_completed_snapshot_without_timeout():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="completed")

    result = asyncio.run(
        runtime.wait_for_tasks(["task-1"], scope_id="main", timeout_seconds=30)
    )

    assert result["timeout"] is False
    assert result["pending_task_ids"] == []
    assert result["tasks"][0]["task_id"] == "task-1"
    assert result["tasks"][0]["status"] == "completed"


def test_wait_for_tasks_soft_timeout_is_not_hard_error():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="running")

    result = asyncio.run(
        runtime.wait_for_tasks(["task-1"], scope_id="main", timeout_seconds=1)
    )

    assert "error" not in result
    assert result["timeout"] is True
    assert result["pending_task_ids"] == ["task-1"]
    assert result["tasks"][0]["status"] == "running"


def test_wait_for_tasks_reports_expired_tasks_separately_from_inaccessible():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="expired-task", parent_scope_id="main", status="completed")
    runtime._unregister_record("expired-task")

    result = asyncio.run(
        runtime.wait_for_tasks(["expired-task"], scope_id="main", timeout_seconds=1)
    )

    assert result["error"].startswith("Error: Task records expired or were already cleaned up:")
    assert result["expired_task_ids"] == ["expired-task"]
    assert result["inaccessible_task_ids"] == []


def test_wait_for_tasks_still_errors_for_inaccessible_task_ids():
    runtime = AgentTaskRuntime()
    _register_task(
        runtime,
        task_id="foreign-task",
        parent_scope_id="foreign-scope",
        status="completed",
    )
    runtime._unregister_record("foreign-task")

    result = asyncio.run(
        runtime.wait_for_tasks(["foreign-task"], scope_id="main", timeout_seconds=1)
    )

    assert result["error"] == "Error: Unknown or inaccessible task ids: foreign-task."
    assert result["inaccessible_task_ids"] == ["foreign-task"]
    assert result["expired_task_ids"] == []


def test_wait_for_tasks_reports_unknown_task_ids_as_inaccessible():
    runtime = AgentTaskRuntime()

    result = asyncio.run(
        runtime.wait_for_tasks(["never-existed"], scope_id="main", timeout_seconds=1)
    )

    assert result["error"] == "Error: Unknown or inaccessible task ids: never-existed."
    assert result["inaccessible_task_ids"] == ["never-existed"]
    assert result["expired_task_ids"] == []


def test_wait_for_tasks_consumes_matching_notifications_to_avoid_double_delivery():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="completed")
    runtime._enqueue_notification_for_record(
        runtime._records["task-1"],
        summary="done",
        result_text="result",
    )

    result = asyncio.run(
        runtime.wait_for_tasks(["task-1"], scope_id="main", timeout_seconds=30)
    )

    assert result["tasks"][0]["task_id"] == "task-1"
    assert runtime.ensure_scope("main").pending_notifications == []


def test_drain_pending_notifications_is_scope_local():
    runtime = AgentTaskRuntime()
    runtime.ensure_scope("child-scope")
    _register_task(
        runtime,
        task_id="child",
        parent_scope_id="child-scope",
        status="completed",
    )
    runtime._enqueue_notification_for_record(
        runtime._records["child"],
        summary="done",
        result_text="result",
    )

    main_drained = asyncio.run(runtime.drain_pending_notifications(scope_id="main"))
    child_drained = asyncio.run(
        runtime.drain_pending_notifications(scope_id="child-scope")
    )

    assert main_drained == []
    assert len(child_drained) == 1
    assert child_drained[0]["metadata"]["task_id"] == "child"


def test_wait_for_tasks_partial_wakeup_rechecks_until_all_stable():
    async def _run():
        runtime = AgentTaskRuntime()
        _register_task(runtime, task_id="task-a", status="running")
        _register_task(runtime, task_id="task-b", status="running")

        async def _complete_a_then_b():
            await asyncio.sleep(0.02)
            runtime._set_record_status(runtime._records["task-a"], "completed")
            runtime._enqueue_notification_for_record(
                runtime._records["task-a"],
                summary="a done",
                result_text="a",
            )
            await asyncio.sleep(0.02)
            runtime._set_record_status(runtime._records["task-b"], "completed")
            runtime._enqueue_notification_for_record(
                runtime._records["task-b"],
                summary="b done",
                result_text="b",
            )

        waiter = asyncio.create_task(
            runtime.wait_for_tasks(
                ["task-a", "task-b"],
                scope_id="main",
                timeout_seconds=1,
            )
        )
        producer = asyncio.create_task(_complete_a_then_b())
        result = await waiter
        await producer
        return result

    result = asyncio.run(_run())

    assert result["timeout"] is False
    assert result["pending_task_ids"] == []
    assert {task["task_id"] for task in result["tasks"]} == {"task-a", "task-b"}
    assert {task["status"] for task in result["tasks"]} == {"completed"}
    assert result["missing_task_ids"] == []


def test_wait_for_tasks_partial_wakeup_keeps_absolute_deadline():
    async def _run():
        runtime = AgentTaskRuntime()
        _register_task(runtime, task_id="task-a", status="running")
        _register_task(runtime, task_id="task-b", status="running")

        async def _complete_a_only():
            await asyncio.sleep(0.6)
            runtime._set_record_status(runtime._records["task-a"], "completed")
            runtime._enqueue_notification_for_record(
                runtime._records["task-a"],
                summary="a done",
                result_text="a",
            )

        started = time.perf_counter()
        waiter = asyncio.create_task(
            runtime.wait_for_tasks(
                ["task-a", "task-b"],
                scope_id="main",
                timeout_seconds=1,
            )
        )
        producer = asyncio.create_task(_complete_a_only())
        result = await waiter
        await producer
        elapsed = time.perf_counter() - started
        return runtime, result, elapsed

    runtime, result, elapsed = asyncio.run(_run())

    assert "error" not in result
    assert result["timeout"] is True
    assert result["pending_task_ids"] == ["task-b"]
    assert runtime.ensure_scope("main").waiters == {}
    assert 0.9 <= elapsed < 1.25


def test_wait_for_tasks_consumes_notifications_after_wakeup():
    async def _run():
        runtime = AgentTaskRuntime()
        _register_task(runtime, task_id="task-a", status="running")

        async def _complete():
            await asyncio.sleep(0.02)
            runtime._set_record_status(runtime._records["task-a"], "completed")
            runtime._enqueue_notification_for_record(
                runtime._records["task-a"],
                summary="done",
                result_text="result",
            )

        waiter = asyncio.create_task(
            runtime.wait_for_tasks(["task-a"], scope_id="main", timeout_seconds=1)
        )
        producer = asyncio.create_task(_complete())
        result = await waiter
        await producer
        drained = await runtime.drain_pending_notifications(scope_id="main")
        return runtime, result, drained

    runtime, result, drained = asyncio.run(_run())

    assert result["tasks"][0]["status"] == "completed"
    assert drained == []
    assert runtime.ensure_scope("main").waiters == {}


def test_worker_zombie_wakeup_does_not_revive_killed_worker():
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, stub = _worker(
            runtime,
            task_id="worker-zombie",
            status="idle",
        )
        worker.start()
        await asyncio.sleep(0)

        stopped = await runtime.stop_task("worker-zombie", scope_id="main")
        assert isinstance(stopped, AgentTaskRecord)
        assert stopped.status == "killed"

        await worker.queue.put({"prompt": "ghost"})
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return record, stub

    record, stub = asyncio.run(_run())

    assert record.status == "killed"
    assert stub.calls == []


def test_worker_invalid_queue_payload_fails_as_runtime_consistency_error():
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, stub = _worker(
            runtime,
            task_id="worker-invalid-payload",
            status="queued",
            reusable=False,
        )
        worker.start()
        await worker.queue.put({"bad": "payload"})
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return runtime, record, stub

    runtime, record, stub = asyncio.run(_run())

    assert record.status == "failed"
    assert record.termination_reason == "internal_inconsistency"
    assert "Invalid worker queue payload" in record.error_text
    assert stub.calls == []


def test_send_message_cancels_idle_ttl_before_old_timer_can_complete(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, _stub = _worker(
            runtime,
            task_id="worker-idle",
            status="idle",
        )

        async with worker.lock:
            worker.idle_epoch = 1
            worker._start_idle_ttl_locked(worker.idle_epoch)
            old_ttl = worker._idle_ttl_task

        sent = await runtime.send_message("worker-idle", scope_id="main", prompt="continue")
        await asyncio.sleep(0.08)
        return record, worker, old_ttl, sent

    monkeypatch.setattr(task_runtime_module, "IDLE_TTL_SECONDS", 0.05)
    record, worker, old_ttl, sent = asyncio.run(_run())

    assert isinstance(sent, AgentTaskRecord)
    assert record.status == "queued"
    assert worker._idle_ttl_task is None
    assert old_ttl is not None and old_ttl.cancelled()


def test_idle_ttl_expiry_cancels_waiting_loop_and_retires_worker(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, _ = _worker(
            runtime,
            task_id="worker-idle-expire",
            status="idle",
        )
        worker.start()
        await asyncio.sleep(0)

        async with worker.lock:
            worker.idle_epoch = 1
            worker._start_idle_ttl_locked(worker.idle_epoch)

        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return runtime, record, worker

    monkeypatch.setattr(task_runtime_module, "IDLE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 5.0)
    runtime, record, worker = asyncio.run(_run())

    assert record.status == "completed"
    assert record.termination_reason == "idle_ttl_expired"
    assert worker.loop_task.done() is True
    assert "worker-idle-expire" not in runtime._workers


def test_non_reusable_worker_is_retired_and_record_cleanup_is_ttl_bound(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, _ = _worker(
            runtime,
            task_id="worker-cleanup",
            status="queued",
            reusable=False,
        )
        worker.start()
        await worker.queue.put({"prompt": "work"})
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        worker_removed_immediately = "worker-cleanup" not in runtime._workers
        await asyncio.sleep(0.08)
        return runtime, record, worker_removed_immediately

    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 0.05)
    runtime, record, worker_removed_immediately = asyncio.run(_run())

    assert record.status == "completed"
    assert worker_removed_immediately is True
    assert "worker-cleanup" not in runtime._records
    assert "worker-cleanup" not in runtime._scopes


def test_idle_stop_cancels_waiting_loop_and_retires_worker(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, _ = _worker(
            runtime,
            task_id="worker-idle-stop-loop",
            status="idle",
        )
        worker.start()
        await asyncio.sleep(0)
        async with worker.lock:
            worker.idle_epoch = 1
            worker._start_idle_ttl_locked(worker.idle_epoch)

        result = await runtime.stop_task("worker-idle-stop-loop", scope_id="main")
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return runtime, record, worker, result

    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 5.0)
    runtime, record, worker, result = asyncio.run(_run())

    assert isinstance(result, AgentTaskRecord)
    assert record.status == "killed"
    assert worker.loop_task.done() is True
    assert worker._idle_ttl_task is None
    assert "worker-idle-stop-loop" not in runtime._workers


def test_stop_task_cleans_up_terminal_worker_loop(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        record, worker, _ = _worker(
            runtime,
            task_id="worker-terminal-stop",
            status="completed",
        )
        worker.start()
        await asyncio.sleep(0)

        result = await runtime.stop_task("worker-terminal-stop", scope_id="main")
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return runtime, record, worker, result

    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 5.0)
    runtime, record, worker, result = asyncio.run(_run())

    assert isinstance(result, AgentTaskRecord)
    assert record.status == "completed"
    assert worker.loop_task.done() is True
    assert "worker-terminal-stop" not in runtime._workers


def test_stop_task_covers_queued_idle_and_running_live_workers(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()

        queued_record, queued_worker, _ = _worker(
            runtime,
            task_id="worker-queued",
            status="queued",
            reusable=False,
        )

        idle_record, idle_worker, _ = _worker(
            runtime,
            task_id="worker-idle-stop",
            status="idle",
        )
        async with idle_worker.lock:
            idle_worker.idle_epoch = 1
            idle_worker._start_idle_ttl_locked(idle_worker.idle_epoch)

        running_started = asyncio.Event()
        release_running = asyncio.Event()

        async def _running_responder(prompt: str, session_state: SubAgentSessionState):
            running_started.set()
            try:
                await release_running.wait()
            except asyncio.CancelledError:
                raise
            return SimpleNamespace(final_text=prompt)

        running_record, running_worker, _ = _worker(
            runtime,
            task_id="worker-running",
            status="queued",
            reusable=False,
            subagent=_StubSubAgent(_running_responder),
        )
        running_worker.start()
        await running_worker.queue.put({"prompt": "work"})
        await asyncio.wait_for(running_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        queued_result = await runtime.stop_task("worker-queued", scope_id="main")
        idle_result = await runtime.stop_task("worker-idle-stop", scope_id="main")
        running_result = await runtime.stop_task("worker-running", scope_id="main")
        await asyncio.wait_for(running_worker.loop_task, timeout=1.0)
        return (
            queued_record,
            queued_worker,
            idle_record,
            idle_worker,
            running_record,
            running_worker,
            queued_result,
            idle_result,
            running_result,
        )

    monkeypatch.setattr(task_runtime_module, "IDLE_TTL_SECONDS", 5.0)
    (
        queued_record,
        queued_worker,
        idle_record,
        idle_worker,
        running_record,
        running_worker,
        queued_result,
        idle_result,
        running_result,
    ) = asyncio.run(_run())

    assert isinstance(queued_result, AgentTaskRecord)
    assert queued_record.status == "killed"
    assert queued_record.termination_reason == "stopped"

    assert isinstance(idle_result, AgentTaskRecord)
    assert idle_record.status == "killed"
    assert idle_record.termination_reason == "stopped"
    assert idle_worker._idle_ttl_task is None

    assert isinstance(running_result, AgentTaskRecord)
    assert running_record.status == "killed"
    assert running_record.termination_reason == "stopped"
    assert running_worker.abort_requested is True
    assert running_worker.loop_task.done() is True


def test_worker_loop_cancellation_cleans_up_active_request_and_marks_interrupted():
    async def _run():
        runtime = AgentTaskRuntime()
        running_started = asyncio.Event()
        request_cancelled = asyncio.Event()

        async def _responder(prompt: str, session_state: SubAgentSessionState):
            running_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise
            return SimpleNamespace(final_text=prompt)

        record, worker, _ = _worker(
            runtime,
            task_id="worker-loop-cancel",
            status="queued",
            reusable=False,
            subagent=_StubSubAgent(_responder),
        )
        worker.start()
        await worker.queue.put({"prompt": "work"})
        await asyncio.wait_for(running_started.wait(), timeout=1.0)

        worker.loop_task.cancel()
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        await asyncio.wait_for(request_cancelled.wait(), timeout=1.0)
        return runtime, record, worker

    runtime, record, worker = asyncio.run(_run())

    assert record.status == "interrupted"
    assert record.termination_reason == "cancelled"
    assert worker.abort_requested is True
    assert worker._first_request_pending is True
    assert worker._current_request_task is None
    assert "worker-loop-cancel" not in runtime._workers


def test_cleanup_scope_owns_worker_teardown_without_leaving_cleanup_tasks(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        runtime.ensure_scope("scope-a")
        record, worker, _ = _worker(
            runtime,
            task_id="worker-scope-cleanup",
            status="idle",
        )
        record.parent_scope_id = "scope-a"
        runtime._child_counts.clear()
        runtime._register_record(record, create_task_scope=True)
        worker.start()
        await asyncio.sleep(0)

        report = await runtime.cleanup_scope("scope-a")
        await asyncio.sleep(0)
        return runtime, report

    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 5.0)
    runtime, report = asyncio.run(_run())

    assert report.tasks_removed == 1
    assert runtime._record_cleanup_tasks == {}
    assert runtime._workers == {}
    assert "worker-scope-cleanup" not in runtime._records


def test_cleanup_scope_repeats_until_late_direct_children_are_removed(monkeypatch):
    async def _run():
        runtime = AgentTaskRuntime()
        runtime.ensure_scope("scope-a")
        first, worker, _ = _worker(
            runtime,
            task_id="worker-first",
            status="idle",
        )
        first.parent_scope_id = "scope-a"
        runtime._child_counts.clear()
        runtime._register_record(first, create_task_scope=True)
        worker.start()
        await asyncio.sleep(0)

        original_cleanup_scope = runtime.cleanup_scope
        injected = False

        async def _wrapped_cleanup(scope_id: str):
            nonlocal injected
            if scope_id == "worker-first" and not injected:
                injected = True
                _register_task(
                    runtime,
                    task_id="worker-late",
                    parent_scope_id="scope-a",
                    status="completed",
                )
            return await original_cleanup_scope(scope_id)

        runtime.cleanup_scope = _wrapped_cleanup  # type: ignore[method-assign]
        report = await original_cleanup_scope("scope-a")
        return runtime, report

    monkeypatch.setattr(task_runtime_module, "TERMINAL_RECORD_TTL_SECONDS", 5.0)
    runtime, report = asyncio.run(_run())

    assert report.tasks_removed == 2
    assert runtime._workers == {}
    assert "worker-first" not in runtime._records
    assert "worker-late" not in runtime._records


def test_cleanup_scope_forgets_expired_identities_for_removed_tasks():
    async def _run():
        runtime = AgentTaskRuntime()
        runtime.ensure_scope("scope-a")
        _register_task(
            runtime,
            task_id="worker-cleanup",
            parent_scope_id="scope-a",
            status="completed",
        )
        report = await runtime.cleanup_scope("scope-a")
        return runtime, report

    runtime, report = asyncio.run(_run())

    assert report.tasks_removed == 1
    assert "worker-cleanup" not in runtime._records
    assert "worker-cleanup" not in runtime._expired_task_parent_scopes


def test_terminal_record_cleanup_waits_for_child_records():
    runtime = AgentTaskRuntime()
    parent = _register_task(runtime, task_id="parent", status="completed")
    child = _register_task(
        runtime,
        task_id="child",
        parent_scope_id="parent",
        status="completed",
    )

    assert runtime._discard_terminal_record("parent") is False
    assert "parent" in runtime._records

    runtime._unregister_record("child")
    runtime._scopes.pop("child", None)

    assert runtime._discard_terminal_record("parent") is True
    assert "parent" not in runtime._records


def test_worker_records_total_tool_uses_across_full_request():
    async def _run():
        runtime = AgentTaskRuntime()

        async def _responder(prompt: str, session_state: SubAgentSessionState):
            session_state.recent_tool_observations = [{"call": "last-turn-only"}]
            session_state.total_tool_use_count += 3
            return SimpleNamespace(final_text=f"done:{prompt}")

        record, worker, _ = _worker(
            runtime,
            task_id="worker-tools",
            status="queued",
            reusable=False,
            subagent=_StubSubAgent(_responder),
        )
        worker.start()
        await worker.queue.put({"prompt": "work"})
        await asyncio.wait_for(worker.loop_task, timeout=1.0)
        return record

    record = asyncio.run(_run())

    assert record.status == "completed"
    assert record.tool_use_count == 3


def test_stop_task_marks_nonlive_record_killed():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="queued")

    result = asyncio.run(runtime.stop_task("task-1", scope_id="main"))

    assert isinstance(result, AgentTaskRecord)
    assert result.status == "killed"
    assert result.termination_reason == "stopped"


def test_stop_task_marks_nonlive_record_killed_and_enqueues_notification():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="task-1", status="queued")

    asyncio.run(runtime.stop_task("task-1", scope_id="main"))
    drained = asyncio.run(runtime.drain_pending_notifications(scope_id="main"))

    assert len(drained) == 1
    assert drained[0]["metadata"]["task_id"] == "task-1"
    assert drained[0]["metadata"]["status"] == "killed"


def test_terminal_record_cleanup_forgets_expired_identity_after_ttl_cleanup():
    runtime = AgentTaskRuntime()
    _register_task(runtime, task_id="done-task", status="completed")

    assert runtime._discard_terminal_record("done-task") is True
    assert "done-task" not in runtime._records
    assert "done-task" not in runtime._expired_task_parent_scopes
