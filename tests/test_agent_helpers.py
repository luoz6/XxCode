"""Tests for helper boundaries used by the core agent loop."""

import asyncio

import pytest

from xxcode.agent.loop import CoreExecutionEngine
from xxcode.agent.continue_reasons import ContinueReason
from xxcode.agent.messages import (
    add_usage,
    commit_assistant_turn,
    commit_tool_results_turn,
)
from xxcode.agent.output_recovery import (
    ESCALATED_MAX_TOKENS,
    OutputRecoveryState,
    handle_output_truncation,
)
from xxcode.agent.permission_resolver import denied_tool_result_content
from xxcode.agent.state import AgentState
from xxcode.tools import ToolCall
from xxcode.tools.registry import ToolRegistry


class _Slot:
    def __init__(self, tc: ToolCall, is_error: bool):
        self.tc = tc
        self.is_error = is_error


class _Executor:
    def __init__(self, slot: _Slot | None):
        self._slot = slot

    def get_slot(self, _tid):
        return self._slot


class _PendingExecutor:
    def __init__(self):
        self.pending = True
        self.wait_calls = 0
        self._registry = ToolRegistry()

    def has_pending_work(self):
        return self.pending

    async def wait_for_activity(self):
        self.wait_calls += 1
        self.pending = False

    def get_completed_results(self):
        return []

    async def get_remaining_results(self):
        return []

    def drain_progress(self):
        return []

    def get_slot(self, _tid):
        return None


def test_commit_assistant_turn_records_usage_and_tool_use():
    state = AgentState()
    tool_call = ToolCall(
        id="tool-1",
        name="read_file",
        input={"file_path": "README.md"},
    )

    commit_assistant_turn(
        state,
        thinking_content=[{"type": "thinking", "thinking": "hmm"}],
        full_text="I will read it.",
        tool_calls=[tool_call],
        message_id="msg-1",
        input_tokens=11,
        output_tokens=7,
    )

    assert state.total_input_tokens == 11
    assert state.total_output_tokens == 7
    assert state.messages == [
        {
            "role": "assistant",
            "id": "msg-1",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "I will read it."},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "read_file",
                    "input": {"file_path": "README.md"},
                },
            ],
        }
    ]


def test_add_usage_accumulates_cache_and_server_tool_tokens():
    state = AgentState()

    add_usage(
        state,
        {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
            "server_tool_use_input_tokens": 2,
        },
    )
    add_usage(
        state,
        {
            "input_tokens": 4,
            "output_tokens": 6,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 2,
            "server_tool_use_input_tokens": 9,
        },
    )

    assert state.total_input_tokens == 15
    assert state.total_output_tokens == 13
    assert state.cache_read_input_tokens == 6
    assert state.cache_creation_input_tokens == 5
    assert state.server_tool_use_input_tokens == 11


def test_agent_state_round_trips_loop_hardening_fields():
    state = AgentState(
        cache_read_input_tokens=10,
        cache_creation_input_tokens=20,
        server_tool_use_input_tokens=30,
        consecutive_autocompact_failures=2,
        last_continue_reason=ContinueReason.NEXT_TURN,
    )

    restored = AgentState.from_dict(state.to_dict())

    assert restored.cache_read_input_tokens == 10
    assert restored.cache_creation_input_tokens == 20
    assert restored.server_tool_use_input_tokens == 30
    assert restored.consecutive_autocompact_failures == 2
    assert restored.last_continue_reason == ContinueReason.NEXT_TURN


def test_commit_tool_results_turn_dedupes_results_and_adds_failure_hint():
    state = AgentState(tool_errors={"read_file": 2})
    tc = ToolCall(id="tool-1", name="read_file", input={"file_path": "missing"})
    executor = _Executor(_Slot(tc, is_error=True))
    tool_results = [
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "missing",
        },
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "duplicate should be dropped",
        },
    ]

    commit_tool_results_turn(state, tool_results, executor)

    assert state.tool_errors["read_file"] == 3
    assert state.messages[0]["role"] == "user"
    content = state.messages[0]["content"]
    assert len(content) == 2
    assert content[0]["content"] == "missing"
    assert "failed 3 times" in content[1]["text"]


def test_output_truncation_escalates_then_commits_continuation():
    state = AgentState()
    recovery = OutputRecoveryState.from_config(api_max_tokens=32)

    first = handle_output_truncation(
        state=state,
        recovery=recovery,
        tool_calls=[],
        thinking_content=[],
        full_text="partial",
        current_message_id="msg-1",
        input_tokens=5,
        output_tokens=6,
    )

    assert first.action == "retry"
    assert recovery.current_max_tokens == ESCALATED_MAX_TOKENS
    assert state.messages == []

    second = handle_output_truncation(
        state=state,
        recovery=recovery,
        tool_calls=[],
        thinking_content=[],
        full_text="partial",
        current_message_id="msg-2",
        input_tokens=7,
        output_tokens=8,
    )

    assert second.action == "continue"
    assert recovery.retries == 1
    assert state.total_input_tokens == 7
    assert state.total_output_tokens == 8
    assert state.messages[0]["id"] == "msg-2"
    assert state.messages[0]["content"] == [{"type": "text", "text": "partial"}]
    assert state.messages[1]["role"] == "user"
    assert "Please continue" in state.messages[1]["content"][0]["text"]


def test_output_truncation_records_structured_continue_reasons():
    state = AgentState()
    recovery = OutputRecoveryState.from_config(api_max_tokens=32)

    first = handle_output_truncation(
        state=state,
        recovery=recovery,
        tool_calls=[],
        thinking_content=[],
        full_text="partial",
        current_message_id="msg-1",
        input_tokens=5,
        output_tokens=6,
    )

    assert first.action == "retry"
    assert first.reason == ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE
    assert state.last_continue_reason == ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE

    second = handle_output_truncation(
        state=state,
        recovery=recovery,
        tool_calls=[],
        thinking_content=[],
        full_text="partial",
        current_message_id="msg-2",
        input_tokens=7,
        output_tokens=8,
    )

    assert second.action == "continue"
    assert second.reason == ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY
    assert state.last_continue_reason == ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY


def test_output_truncation_with_tool_calls_proceeds_without_message_mutation():
    state = AgentState()
    recovery = OutputRecoveryState.from_config(api_max_tokens=32)
    tool_call = ToolCall(id="tool-1", name="read_file", input={"file_path": "x"})

    result = handle_output_truncation(
        state=state,
        recovery=recovery,
        tool_calls=[tool_call],
        thinking_content=[],
        full_text="",
        current_message_id="msg-1",
        input_tokens=5,
        output_tokens=6,
    )

    assert result.action == "proceed"
    assert state.messages == []
    assert recovery.current_max_tokens == 32


def test_denied_tool_result_content_escalates_after_repeated_denials():
    state = AgentState(denied_tool_calls={"write_file": 2})
    tc = ToolCall(id="tool-1", name="write_file", input={})

    content = denied_tool_result_content(state, tc)

    assert state.denied_tool_calls["write_file"] == 3
    assert "denied 'write_file' 3 times" in content
    assert "DO NOT request it again" in content


@pytest.mark.asyncio
async def test_execute_and_commit_tools_waits_for_executor_activity_instead_of_sleep(monkeypatch):
    async def _unexpected_sleep(_seconds):
        raise AssertionError("busy-poll sleep should not be used")

    monkeypatch.setattr("xxcode.agent.loop.asyncio.sleep", _unexpected_sleep)

    engine = CoreExecutionEngine()
    state = AgentState()
    executor = _PendingExecutor()

    async for _event in engine._execute_and_commit_tools(state, executor, []):
        pass

    assert executor.wait_calls == 1
