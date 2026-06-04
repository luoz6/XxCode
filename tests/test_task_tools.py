"""Tests for task runtime tools and Agent background integration."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import xxcode.agent.subagent as subagent_module
import xxcode.tools.agent.tool as agent_tool_module
from xxcode.agent.definitions import build_filtered_registry, get_agent_definition
from xxcode.agent.task_runtime import AgentTaskRecord, AgentTaskRuntime, TaskNotification
from xxcode.tools.agent.tool import AgentInput, AgentTool
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.registry import ToolRegistry
from xxcode.tools.tasks import (
    SendMessageInput,
    SendMessageTool,
    TaskGetInput,
    TaskGetTool,
    TaskListInput,
    TaskListTool,
    TaskStopInput,
    TaskStopTool,
    TaskWaitTool,
)


def _make_config(tmp_path):
    return SimpleNamespace(
        cwd=tmp_path,
        auto_memory_enabled=False,
        api_model="fake-model",
        api_key="fake-key",
        api_base_url="http://fake",
        api_max_tokens=1000,
        max_tool_output_chars=1000,
        session_dir=tmp_path / "sessions",
    )


def _context(tmp_path, runtime: AgentTaskRuntime, registry: ToolRegistry | None = None):
    return {
        "config": _make_config(tmp_path),
        "_registry": registry or ToolRegistry(),
        "task_runtime": runtime,
        "scope_id": "main",
        "current_task_id": None,
        "parent_state": None,
    }


def _make_record(
    task_id: str,
    *,
    parent_task_id: str | None = None,
    parent_scope_id: str = "main",
    worker_label: str | None = None,
    description: str = "desc",
    agent_type: str = "general-purpose",
    reusable: bool = False,
    status: str = "completed",
    created_at: float = 1.0,
    updated_at: float | None = None,
    result_text: str | None = None,
) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        parent_task_id=parent_task_id,
        parent_scope_id=parent_scope_id,
        worker_label=worker_label or task_id,
        description=description,
        agent_type=agent_type,
        reusable=reusable,
        status=status,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        result_text=result_text,
    )


def _register_record(runtime: AgentTaskRuntime, task_id: str, **overrides) -> AgentTaskRecord:
    record = _make_record(task_id, **overrides)
    runtime._register_record(record, create_task_scope=True)
    return record


def _read_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    return registry


def _run_tool(tool, input_model, ctx):
    return asyncio.run(tool.execute(input_model, ctx))


def test_task_wait_schema_requires_timeout_and_documents_recommended_value():
    schema = TaskWaitTool.input_schema.model_json_schema()
    assert "timeout_seconds" in schema["required"]
    assert "Usually set this to at least 300 (5 minutes) for code tasks." in schema["properties"]["timeout_seconds"]["description"]


def test_task_list_and_get_are_scope_bound(tmp_path):
    runtime = AgentTaskRuntime()
    _register_record(
        runtime,
        "task-a",
        worker_label="Task A",
        result_text="done",
    )
    _register_record(
        runtime,
        "task-b",
        parent_scope_id="scope-2",
        worker_label="Task B",
        created_at=2.0,
    )

    ctx = _context(tmp_path, runtime)
    task_list = json.loads(_run_tool(TaskListTool(), TaskListInput(), ctx))
    assert [task["task_id"] for task in task_list["tasks"]] == ["task-a"]

    task_get = json.loads(_run_tool(TaskGetTool(), TaskGetInput(task_id="task-a"), ctx))
    assert task_get["task_id"] == "task-a"

    missing = _run_tool(TaskGetTool(), TaskGetInput(task_id="task-b"), ctx)
    assert "not found" in missing.lower()


def test_agent_background_spawn_returns_task_id_and_task_tools_see_it(tmp_path):
    runtime = AgentTaskRuntime()
    registry = _read_registry()
    agent_tool = AgentTool()

    calls: list[dict[str, object]] = []

    async def _fake_spawn_worker(**kwargs):
        calls.append(kwargs)
        return _make_record(
            "subagent-general-purpose-1234abcd",
            parent_scope_id="main",
            worker_label=str(kwargs["worker_label"]),
            description=str(kwargs["description"]),
            agent_type=str(kwargs["agent_type"]),
            reusable=bool(kwargs["reusable"]),
            status="queued",
        )

    runtime.spawn_worker = _fake_spawn_worker  # type: ignore[method-assign]
    ctx = _context(tmp_path, runtime, registry)
    result = asyncio.run(
        agent_tool.execute(
            AgentInput(
                description="Do work",
                prompt="Handle this task",
                run_in_background=True,
                reusable=True,
            ),
            ctx,
        )
    )

    assert "task_id: subagent-general-purpose-1234abcd" in result
    assert "reusable: true" in result
    assert calls[0]["parent_scope_id"] == "main"
    assert calls[0]["worker_label"] == "Do work"


def test_agent_background_spawn_rejects_duplicate_active_worker_labels(tmp_path):
    runtime = AgentTaskRuntime()
    _register_record(
        runtime,
        "existing",
        worker_label="Do work",
        description="existing",
        status="running",
    )
    registry = _read_registry()

    result = asyncio.run(
        AgentTool().execute(
            AgentInput(
                description="Do work",
                prompt="Handle this task",
                run_in_background=True,
            ),
            _context(tmp_path, runtime, registry),
        )
    )

    assert "same worker_label already exists" in result


def test_agent_background_spawn_allows_reusing_label_from_terminal_task(tmp_path):
    runtime = AgentTaskRuntime()
    _register_record(
        runtime,
        "existing",
        worker_label="Do work",
        description="existing",
        status="completed",
    )
    registry = _read_registry()
    agent_tool = AgentTool()

    async def _fake_spawn_worker(**kwargs):
        return _make_record(
            "subagent-general-purpose-5678efgh",
            parent_scope_id="main",
            worker_label=str(kwargs["worker_label"]),
            description=str(kwargs["description"]),
            agent_type=str(kwargs["agent_type"]),
            reusable=bool(kwargs["reusable"]),
            status="queued",
            created_at=2.0,
        )

    runtime.spawn_worker = _fake_spawn_worker  # type: ignore[method-assign]

    result = asyncio.run(
        agent_tool.execute(
            AgentInput(
                description="Do work",
                prompt="Handle this task",
                run_in_background=True,
            ),
            _context(tmp_path, runtime, registry),
        )
    )

    assert "task_id: subagent-general-purpose-5678efgh" in result


def test_agent_background_spawn_rejects_scope_worker_flood(tmp_path, monkeypatch):
    runtime = AgentTaskRuntime()
    _register_record(
        runtime,
        "existing",
        worker_label="Existing worker",
        description="existing",
        status="running",
    )
    registry = _read_registry()
    monkeypatch.setattr(agent_tool_module, "MAX_BACKGROUND_WORKERS_PER_SCOPE", 1)

    result = asyncio.run(
        AgentTool().execute(
            AgentInput(
                description="Do work",
                prompt="Handle this task",
                worker_label="Unique worker",
                run_in_background=True,
            ),
            _context(tmp_path, runtime, registry),
        )
    )

    assert "Too many active background workers" in result


def test_task_stop_and_send_message_delegate_to_runtime(tmp_path):
    runtime = AgentTaskRuntime()
    ctx = _context(tmp_path, runtime)
    stopped_record = _make_record(
        "task-stop",
        worker_label="stop",
        description="stop",
        status="killed",
    )
    queued_record = _make_record(
        "task-send",
        worker_label="send",
        description="send",
        reusable=True,
        status="queued",
        created_at=2.0,
    )

    async def _fake_stop(task_id: str, scope_id: str, **kwargs):
        assert task_id == "task-stop"
        assert scope_id == "main"
        return stopped_record

    async def _fake_send(task_id: str, scope_id: str, prompt: str):
        assert task_id == "task-send"
        assert scope_id == "main"
        assert prompt == "continue"
        return queued_record

    runtime.stop_task = _fake_stop  # type: ignore[method-assign]
    runtime.send_message = _fake_send  # type: ignore[method-assign]

    stop_result = json.loads(_run_tool(TaskStopTool(), TaskStopInput(task_id="task-stop"), ctx))
    assert stop_result["status"] == "killed"

    send_result = json.loads(
        _run_tool(
            SendMessageTool(),
            SendMessageInput(task_id="task-send", prompt="continue"),
            ctx,
        )
    )
    assert send_result["status"] == "queued"


def test_coordinator_registry_excludes_read_tools_and_only_keeps_task_pool():
    base_registry = ToolRegistry()
    base_registry.register(ReadFileTool())
    base_registry.register(TaskListTool())
    base_registry.register(TaskGetTool())
    base_registry.register(TaskWaitTool())
    base_registry.register(TaskStopTool())
    base_registry.register(SendMessageTool())
    base_registry.register(AgentTool())

    filtered = build_filtered_registry(base_registry, get_agent_definition("Coordinator"))
    tool_names = sorted(tool.name for tool in filtered.list_tools())

    assert tool_names == [
        "Agent",
        "SendMessage",
        "TaskGet",
        "TaskList",
        "TaskStop",
        "TaskWait",
    ]
    assert "read_file" not in tool_names


def test_coordinator_cannot_spawn_sync_workers(tmp_path):
    runtime = AgentTaskRuntime()
    registry = _read_registry()
    tool = AgentTool()

    result = asyncio.run(
        tool.execute(
            AgentInput(
                description="Spawn worker",
                prompt="Do the thing",
                subagent_type="general-purpose",
                run_in_background=False,
            ),
            {
                **_context(tmp_path, runtime, registry),
                "current_agent_type": "Coordinator",
            },
        )
    )

    assert "run_in_background=true" in result


def test_notifications_are_routed_only_to_direct_parent_scope():
    runtime = AgentTaskRuntime()
    runtime.ensure_scope("coordinator-scope")

    record = _make_record(
        "worker-1",
        parent_task_id="coordinator-task",
        parent_scope_id="coordinator-scope",
        worker_label="Worker",
        description="Child worker",
        result_text="done",
    )

    runtime._enqueue_notification_for_record(
        record,
        summary="Worker completed request.",
        result_text="done",
    )

    main_scope = runtime.ensure_scope("main")
    coordinator_scope = runtime.ensure_scope("coordinator-scope")

    assert main_scope.pending_notifications == []
    assert len(coordinator_scope.pending_notifications) == 1
    notification = coordinator_scope.pending_notifications[0]
    assert isinstance(notification, TaskNotification)
    assert notification.parent_scope_id == "coordinator-scope"


def test_sync_subagent_cleans_up_background_workers_before_return(tmp_path, monkeypatch):
    runtime = AgentTaskRuntime()
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(AgentTool())
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskWaitTool())
    registry.register(TaskStopTool())
    registry.register(SendMessageTool())

    worker_gate = asyncio.Event()

    def _latest_user_text(messages: list[dict]) -> str:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            text_parts = [
                block.get("text", "")
                for block in message.get("content", [])
                if block.get("type") == "text"
            ]
            if text_parts:
                return "\n".join(text_parts)
        return ""

    def _tool_result_texts(messages: list[dict]) -> list[str]:
        results: list[str] = []
        for message in messages:
            if message.get("role") != "user":
                continue
            for block in message.get("content", []):
                if block.get("type") == "tool_result":
                    results.append(str(block.get("content", "")))
        return results

    class _Client:
        async def stream_chat(self, system_prompt, messages, tools):
            del system_prompt, tools
            prompt = _latest_user_text(messages)
            tool_results = _tool_result_texts(messages)
            if "worker body" in prompt:
                yield {"type": "message_id", "id": "worker-msg"}
                await worker_gate.wait()
                yield {"type": "text_delta", "text": "worker done"}
                yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
                yield {"type": "stop_reason", "stop_reason": "end_turn"}
                return

            if not tool_results:
                yield {"type": "message_id", "id": "parent-msg-1"}
                yield {
                    "type": "tool_use",
                    "id": "spawn-worker",
                    "name": "Agent",
                    "input": {
                        "description": "Child worker",
                        "prompt": "worker body",
                        "subagent_type": "general-purpose",
                        "run_in_background": True,
                    },
                }
                yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
                yield {"type": "stop_reason", "stop_reason": "tool_use"}
                return

            yield {"type": "message_id", "id": "parent-msg-2"}
            yield {"type": "text_delta", "text": "done early"}
            yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
            yield {"type": "stop_reason", "stop_reason": "end_turn"}

    async def _fake_build_system_prompt(self):
        del self
        return "test system prompt"

    monkeypatch.setattr(subagent_module, "APIClient", lambda **kwargs: _Client())
    monkeypatch.setattr(subagent_module.SubAgent, "_build_system_prompt", _fake_build_system_prompt)

    result = asyncio.run(
        AgentTool().execute(
            AgentInput(
                description="Launch worker then exit",
                prompt="launch worker and exit",
                subagent_type="general-purpose",
                run_in_background=False,
            ),
            _context(tmp_path, runtime, registry),
        )
    )

    assert "background child tasks settled" in result
    assert "Stopped and discarded 1 background task(s)." in result
    assert runtime._records == {}
    assert sorted(runtime._scopes) == ["main"]
