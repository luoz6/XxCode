"""Tests for request-scoped SubAgent execution state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from xxcode.agent.loop import CoreExecutionEngine
from xxcode.agent.subagent import (
    SubAgent,
    SubAgentRequestResult,
    SubAgentSessionState,
)
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.registry import ToolRegistry


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


def _make_definition():
    return SimpleNamespace(
        name="test-agent",
        description="Test agent.",
        model=None,
        max_turns=3,
    )


def test_subagent_session_state_uses_isolated_mutable_defaults():
    first = SubAgentSessionState()
    second = SubAgentSessionState()

    first.messages.append({"role": "user", "content": []})
    first.surfaced_memory_ids.add("one")
    first.recent_tool_observations.append({"call": "x"})
    first.total_tool_use_count += 2

    assert second.messages == []
    assert second.surfaced_memory_ids == set()
    assert second.recent_tool_observations == []
    assert second.total_tool_use_count == 0
    assert first.abort_check is not second.abort_check
    assert first.abort_check() is False
    assert second.abort_check() is False


def test_subagent_run_wraps_single_request_result(tmp_path):
    sub = SubAgent(
        config=_make_config(tmp_path),
        registry=ToolRegistry(),
        definition=_make_definition(),
    )

    session_state = SubAgentSessionState()
    captured: dict[str, object] = {}

    async def _fake_create(prompt: str):
        captured["created_prompt"] = prompt
        return session_state

    async def _fake_execute(prompt: str, supplied_state: SubAgentSessionState):
        captured["executed_prompt"] = prompt
        captured["state"] = supplied_state
        return SubAgentRequestResult(
            final_text="finished",
            total_input_tokens=11,
            total_output_tokens=7,
        )

    sub._create_session_state = _fake_create  # type: ignore[method-assign]
    sub._execute_one_request = _fake_execute  # type: ignore[method-assign]

    result = asyncio.run(sub.run("do work"))

    assert result == "finished"
    assert captured["created_prompt"] == "do work"
    assert captured["executed_prompt"] == "do work"
    assert captured["state"] is session_state
    assert sub.tokens_used == (11, 7)


def test_subagent_create_session_state_honors_abort_check_and_scope_context(tmp_path):
    abort_calls: list[bool] = []

    def _abort_check() -> bool:
        abort_calls.append(True)
        return False

    sub = SubAgent(
        config=_make_config(tmp_path),
        registry=ToolRegistry(),
        definition=_make_definition(),
        extra_context={
            "scope_id": "scope-1",
            "current_task_id": "task-1",
            "abort_check": _abort_check,
        },
    )

    session_state = asyncio.run(sub._create_session_state("hello"))

    assert session_state.scope_id == "scope-1"
    assert session_state.current_task_id == "task-1"
    assert session_state.abort_check() is False
    assert abort_calls == [True]


def test_subagent_continuation_turn_skips_notification_drain(tmp_path):
    sub = SubAgent(
        config=_make_config(tmp_path),
        registry=ToolRegistry(),
        definition=_make_definition(),
    )

    session_state = SubAgentSessionState(
        messages=[{"role": "user", "content": [{"type": "text", "text": "start"}]}],
        system_prompt="system",
    )

    drain_calls: list[tuple[str, str]] = []

    async def _fake_drain(_state):
        drain_calls.append((_state.scope_id, _state.current_task_id))

    class _Client:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "message_id", "id": "m1"}
                yield {"type": "text_delta", "text": "part 1"}
                yield {"type": "usage", "input_tokens": 2, "output_tokens": 3}
                yield {"type": "stop_reason", "stop_reason": "max_tokens"}
                return
            yield {"type": "message_id", "id": "m2"}
            yield {"type": "text_delta", "text": "part 2"}
            yield {"type": "usage", "input_tokens": 5, "output_tokens": 7}
            yield {"type": "stop_reason", "stop_reason": "end_turn"}

    client = _Client()
    sub._drain_pending_notifications = _fake_drain  # type: ignore[method-assign]

    from xxcode.agent import subagent as subagent_module

    original_client = subagent_module.APIClient
    subagent_module.APIClient = lambda **kwargs: client  # type: ignore[assignment]
    try:
        result = asyncio.run(sub._execute_one_request("start", session_state))
    finally:
        subagent_module.APIClient = original_client

    assert result.final_text == "part 2"
    assert result.total_input_tokens == 7
    assert result.total_output_tokens == 10
    assert len(drain_calls) == 1


def test_subagent_read_like_detection_matches_main_loop_location_hint_gate():
    read_tool = ReadFileTool()

    assert SubAgent._is_read_like_tool("read_file", read_tool, {}) is False
    assert CoreExecutionEngine._is_read_like_tool("read_file", read_tool, {}) is False

    raw_input = {"file_path": "README.md"}
    assert SubAgent._is_read_like_tool("read_file", read_tool, raw_input) is True
    assert CoreExecutionEngine._is_read_like_tool("read_file", read_tool, raw_input) is True
