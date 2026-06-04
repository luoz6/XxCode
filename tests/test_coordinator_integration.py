"""End-to-end coverage for Coordinator orchestration behavior."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from itertools import count
from types import SimpleNamespace

import xxcode.agent.subagent as subagent_module
import xxcode.tools.agent.tool as agent_tool_module
from xxcode.agent.definitions import get_agent_definition
from xxcode.agent.task_runtime import AgentTaskRuntime
from xxcode.tools.agent.tool import AgentInput, AgentTool
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.registry import ToolRegistry
from xxcode.tools.tasks import (
    SendMessageTool,
    TaskGetTool,
    TaskListTool,
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


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(AgentTool())
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskWaitTool())
    registry.register(TaskStopTool())
    registry.register(SendMessageTool())
    return registry


def _main_context(tmp_path, runtime: AgentTaskRuntime, registry: ToolRegistry) -> dict[str, object]:
    return {
        "config": _make_config(tmp_path),
        "_registry": registry,
        "task_runtime": runtime,
        "scope_id": "main",
        "current_task_id": None,
        "parent_state": None,
    }


def _tool_result_contents(messages: list[dict]) -> list[str]:
    contents: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                contents.append(str(block.get("content", "")))
    return contents


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


def _parse_spawned_task_ids(contents: list[str]) -> list[str]:
    task_ids: list[str] = []
    for content in contents:
        match = re.search(r"task_id:\s*([^\s]+)", content)
        if match:
            task_ids.append(match.group(1))
    return task_ids


def _json_payloads(contents: list[str]) -> list[dict]:
    payloads: list[dict] = []
    for content in contents:
        stripped = content.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


class _CoordinatorWorkerClient:
    _counter = count(1)

    async def stream_chat(self, system_prompt, messages, tools):
        del system_prompt
        prompt = _latest_user_text(messages).lower()
        if "alpha task" in prompt or "beta task" in prompt:
            async for event in self._worker_stream(messages):
                yield event
            return
        async for event in self._coordinator_stream(messages):
            yield event

    async def _coordinator_stream(self, messages: list[dict]):
        contents = _tool_result_contents(messages)
        task_ids = _parse_spawned_task_ids(contents)
        payloads = _json_payloads(contents)
        task_get_payloads = [
            payload
            for payload in payloads
            if "task_id" in payload and "result_text" in payload and "status" in payload
        ]

        if not any("Unknown tool: 'read_file'" in content for content in contents):
            async for event in self._tool_events(
                [("read_file", {"file_path": "README.md"})]
            ):
                yield event
            return

        if not task_ids:
            async for event in self._tool_events(
                [
                    (
                        "Agent",
                        {
                            "description": "Alpha worker",
                            "prompt": "alpha task",
                            "subagent_type": "general-purpose",
                            "run_in_background": True,
                        },
                    ),
                    (
                        "Agent",
                        {
                            "description": "Beta worker",
                            "prompt": "beta task",
                            "subagent_type": "general-purpose",
                            "run_in_background": True,
                        },
                    ),
                ]
            ):
                yield event
            return

        if not any("timeout" in payload for payload in payloads):
            async for event in self._tool_events(
                [("TaskWait", {"task_ids": task_ids, "timeout_seconds": 300})]
            ):
                yield event
            return

        if len(task_get_payloads) < len(task_ids):
            async for event in self._tool_events(
                [("TaskGet", {"task_id": task_id}) for task_id in task_ids]
            ):
                yield event
            return

        summary = "; ".join(payload["result_text"] for payload in task_get_payloads)
        async for event in self._text_events(
            f"Recovered after read_file rejection. Final summary: {summary}"
        ):
            yield event

    async def _worker_stream(self, messages: list[dict]):
        prompt = _latest_user_text(messages).lower()
        if "alpha" in prompt:
            final_text = "alpha complete"
        elif "beta" in prompt:
            final_text = "beta complete"
        else:
            final_text = "worker complete"
        async for event in self._text_events(final_text):
            yield event

    async def _tool_events(self, calls: list[tuple[str, dict]]):
        yield {"type": "message_id", "id": f"msg-{next(self._counter)}"}
        for name, payload in calls:
            yield {
                "type": "tool_use",
                "id": f"tool-{next(self._counter)}",
                "name": name,
                "input": payload,
            }
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "tool_use"}

    async def _text_events(self, text: str):
        yield {"type": "message_id", "id": f"msg-{next(self._counter)}"}
        yield {"type": "text_delta", "text": text}
        yield {"type": "usage", "input_tokens": 2, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _StubbornCoordinatorClient:
    _counter = count(10_000)

    async def stream_chat(self, system_prompt, messages, tools):
        del system_prompt, messages, tools
        yield {"type": "message_id", "id": f"msg-{next(self._counter)}"}
        yield {
            "type": "tool_use",
            "id": f"tool-{next(self._counter)}",
            "name": "read_file",
            "input": {"file_path": "README.md"},
        }
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "tool_use"}


async def _fake_build_system_prompt(self):
    del self
    return "test system prompt"


def test_coordinator_recovers_from_unauthorized_tool_and_keeps_notifications_scoped(tmp_path, monkeypatch):
    runtime = AgentTaskRuntime()
    registry = _make_registry()
    tool = AgentTool()

    monkeypatch.setattr(subagent_module, "APIClient", lambda **kwargs: _CoordinatorWorkerClient())
    monkeypatch.setattr(subagent_module.SubAgent, "_build_system_prompt", _fake_build_system_prompt)

    result = asyncio.run(
        tool.execute(
            AgentInput(
                description="Coordinate workers",
                prompt="Spawn workers and summarize their results.",
                subagent_type="Coordinator",
                run_in_background=False,
            ),
            _main_context(tmp_path, runtime, registry),
        )
    )

    assert "Recovered after read_file rejection." in result
    assert "alpha complete" in result
    assert "beta complete" in result
    assert "<task-notification>" not in result
    assert "<persisted-output>" not in result

    main_drained = asyncio.run(runtime.drain_pending_notifications(scope_id="main"))
    assert runtime._records == {}
    assert sorted(runtime._scopes) == ["main"]

    assert main_drained == []


def test_coordinator_stubborn_hallucination_exits_at_low_max_turns(tmp_path, monkeypatch):
    runtime = AgentTaskRuntime()
    registry = _make_registry()
    tool = AgentTool()
    original_definition = get_agent_definition("Coordinator")

    monkeypatch.setattr(subagent_module, "APIClient", lambda **kwargs: _StubbornCoordinatorClient())
    monkeypatch.setattr(subagent_module.SubAgent, "_build_system_prompt", _fake_build_system_prompt)
    monkeypatch.setattr(
        agent_tool_module,
        "get_agent_definition",
        lambda subagent_type: (
            replace(original_definition, max_turns=3)
            if subagent_type == "Coordinator"
            else get_agent_definition(subagent_type)
        ),
    )

    result = asyncio.run(
        tool.execute(
            AgentInput(
                description="Coordinate stubborn worker plan",
                prompt="Keep coordinating.",
                subagent_type="Coordinator",
                run_in_background=False,
            ),
            _main_context(tmp_path, runtime, registry),
        )
    )

    assert "Sub-agent reached maximum turns (3)." in result
    assert "Unknown tool: 'read_file'" not in result
