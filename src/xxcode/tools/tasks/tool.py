"""Task management tools for the multi-agent runtime."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ...agent.task_runtime import AgentTaskRecord
from .. import Tool

_TASK_TEXT_PREVIEW_CHARS = 4_000


def _require_runtime_and_scope(
    context: dict[str, Any],
) -> tuple[Any | None, str, str | None]:
    runtime = context.get("task_runtime")
    scope_id = str(context.get("scope_id", "main") or "main")
    if runtime is None:
        return None, scope_id, "Error: task runtime is not available in the execution context."
    return runtime, scope_id, None


def _serialize_task_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    for field_name in ("result_text", "error_text"):
        value = payload.get(field_name)
        if not isinstance(value, str):
            continue
        total_chars = len(value)
        if total_chars <= _TASK_TEXT_PREVIEW_CHARS:
            continue
        payload[field_name] = value[:_TASK_TEXT_PREVIEW_CHARS] + "\n...[truncated]"
        payload[f"{field_name}_truncated"] = True
        payload[f"{field_name}_chars_total"] = total_chars
    return payload


def _record_to_json(record: AgentTaskRecord) -> str:
    return json.dumps(
        _serialize_task_payload(record.to_dict()),
        ensure_ascii=False,
        indent=2,
    )


class _TaskRuntimeTool(Tool):
    _is_read_only = False
    _is_concurrency_safe = True
    _is_destructive = False

    def needs_permission(self, input: BaseModel) -> bool:
        return False

    async def format_large_result(
        self,
        content: str,
        max_chars: int,
        tool_use_id: str = "",
        session_dir: str = "",
    ) -> str:
        del max_chars, tool_use_id, session_dir
        # Task orchestration tools must remain machine-readable JSON.
        return content


class TaskListInput(BaseModel):
    """No-argument schema for listing current-scope tasks."""


class TaskListTool(_TaskRuntimeTool):
    name = "TaskList"
    description = (
        "List all direct child tasks spawned by the current agent scope, including "
        "their status, usage, and result metadata."
    )
    input_schema = TaskListInput

    async def execute(self, input: TaskListInput, context: dict[str, Any]) -> str:
        runtime, scope_id, error = _require_runtime_and_scope(context)
        if error is not None:
            return error
        tasks = [
            _serialize_task_payload(record.to_dict())
            for record in runtime.list_tasks(scope_id)
        ]
        return json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2)

    def render_tool_use(self, input: BaseModel) -> str:
        return "TaskList()"


class TaskGetInput(BaseModel):
    task_id: str = Field(description="The system-generated task_id to inspect.")


class TaskGetTool(_TaskRuntimeTool):
    name = "TaskGet"
    description = (
        "Get the latest details for one direct child task by task_id, including "
        "status, result text, error text, usage, and lifecycle metadata."
    )
    input_schema = TaskGetInput

    async def execute(self, input: TaskGetInput, context: dict[str, Any]) -> str:
        runtime, scope_id, error = _require_runtime_and_scope(context)
        if error is not None:
            return error
        record = runtime.get_task(input.task_id, scope_id)
        if record is None:
            return f"Error: Task '{input.task_id}' was not found in the current scope."
        return _record_to_json(record)

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, TaskGetInput)
        return f"TaskGet({input.task_id})"


class TaskWaitInput(BaseModel):
    task_ids: list[str] = Field(
        description="One or more direct child task_ids to wait for.",
        min_length=1,
    )
    timeout_seconds: int = Field(
        description=(
            "Soft timeout in seconds for waiting on these tasks. Usually set this "
            "to at least 300 (5 minutes) for code tasks."
        ),
        ge=1,
    )


class TaskWaitTool(_TaskRuntimeTool):
    name = "TaskWait"
    description = (
        "Block without busy-polling until all specified direct child tasks reach a "
        "stable state, or until timeout_seconds elapses."
    )
    input_schema = TaskWaitInput

    async def execute(self, input: TaskWaitInput, context: dict[str, Any]) -> str:
        runtime, scope_id, error = _require_runtime_and_scope(context)
        if error is not None:
            return error
        result = await runtime.wait_for_tasks(
            task_ids=input.task_ids,
            scope_id=scope_id,
            timeout_seconds=input.timeout_seconds,
        )
        if isinstance(result.get("tasks"), list):
            result["tasks"] = [
                _serialize_task_payload(task)
                if isinstance(task, dict)
                else task
                for task in result["tasks"]
            ]
        return json.dumps(result, ensure_ascii=False, indent=2)

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, TaskWaitInput)
        return f"TaskWait({len(input.task_ids)} tasks, timeout={input.timeout_seconds}s)"


class TaskStopInput(BaseModel):
    task_id: str = Field(description="The direct child task_id to stop.")


class TaskStopTool(_TaskRuntimeTool):
    name = "TaskStop"
    description = (
        "Stop one direct child task. Queued workers are killed immediately; "
        "running workers are cancelled and converge to killed."
    )
    input_schema = TaskStopInput

    async def execute(self, input: TaskStopInput, context: dict[str, Any]) -> str:
        runtime, scope_id, error = _require_runtime_and_scope(context)
        if error is not None:
            return error
        result = await runtime.stop_task(input.task_id, scope_id)
        if isinstance(result, str):
            return result
        return _record_to_json(result)

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, TaskStopInput)
        return f"TaskStop({input.task_id})"


class SendMessageInput(BaseModel):
    task_id: str = Field(
        description="The reusable direct child worker task_id to continue.",
    )
    prompt: str = Field(
        description="The next instruction to send to that idle worker.",
    )


class SendMessageTool(_TaskRuntimeTool):
    name = "SendMessage"
    description = (
        "Send a new prompt to an existing reusable worker. This only succeeds when "
        "the target worker is idle and ready for more work."
    )
    input_schema = SendMessageInput

    async def execute(self, input: SendMessageInput, context: dict[str, Any]) -> str:
        runtime, scope_id, error = _require_runtime_and_scope(context)
        if error is not None:
            return error
        result = await runtime.send_message(input.task_id, scope_id, input.prompt)
        if isinstance(result, str):
            return result
        return _record_to_json(result)

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, SendMessageInput)
        return f"SendMessage({input.task_id})"
