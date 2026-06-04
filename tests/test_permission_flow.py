"""Tests for interactive permission handling in the core agent loop."""

import asyncio
import copy
from pathlib import Path

from xxcode.agent.events import StreamEvent
from xxcode.agent.loop import CoreExecutionEngine
from xxcode.agent.state import AgentState
from xxcode.config import Config


class _FakeClient:
    """Streaming client that asks the agent to write one file, then finishes."""

    def __init__(self, target: Path):
        self.target = target
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-1"}
            yield {
                "type": "tool_use",
                "id": "tool-1",
                "name": "write_file",
                "input": {
                    "file_path": str(self.target),
                    "content": "written by test",
                },
            }
            yield {"type": "usage", "input_tokens": 10, "output_tokens": 5}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return

        yield {"type": "message_id", "id": "msg-2"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _TruncatedToolUseClient:
    """Client that emits a complete tool_use with a max_tokens stop reason."""

    def __init__(self, target: Path):
        self.target = target
        self.calls = 0
        self.messages_by_call: list[list[dict]] = []

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.messages_by_call.append(copy.deepcopy(messages))

        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-read"}
            yield {
                "type": "tool_use",
                "id": "tool-read-1",
                "name": "read_file",
                "input": {
                    "file_path": str(self.target),
                },
            }
            yield {"type": "usage", "input_tokens": 7, "output_tokens": 4}
            yield {"type": "stop_reason", "stop_reason": "max_tokens"}
            return

        yield {"type": "message_id", "id": "msg-final"}
        yield {"type": "text_delta", "text": "read complete"}
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _DangerousShellClient:
    """Client that requests a dangerous shell command requiring permission."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-shell"}
            yield {
                "type": "tool_use",
                "id": "tool-shell-1",
                "name": "run_shell",
                "input": {
                    "command": "sudo rm -rf /tmp/xxcode-risk-test",
                },
            }
            yield {"type": "usage", "input_tokens": 8, "output_tokens": 4}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return

        yield {"type": "message_id", "id": "msg-final"}
        yield {"type": "text_delta", "text": "permission denied"}
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _CaptureFirstCallClient:
    """Client that records the first message payload and then finishes."""

    def __init__(self):
        self.messages_by_call: list[list[dict]] = []

    async def stream_chat(self, system_prompt, messages, tools):
        self.messages_by_call.append(copy.deepcopy(messages))
        yield {"type": "message_id", "id": "msg-memory"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _RecallAwareClient:
    """Client that supports both main streaming and recall side-queries."""

    def __init__(self, target: Path):
        self.target = target
        self.stream_calls = 0
        self.complete_calls = 0
        self.messages_by_stream_call: list[list[dict]] = []
        self.recall_messages: list[list[dict]] = []

    async def complete(
        self,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        self.complete_calls += 1
        self.recall_messages.append(copy.deepcopy(messages or []))
        if self.complete_calls == 1:
            return '["initial.md"]'
        return '["initial.md", "followup.md"]'

    async def stream_chat(self, system_prompt, messages, tools):
        self.stream_calls += 1
        self.messages_by_stream_call.append(copy.deepcopy(messages))

        if self.stream_calls == 1:
            yield {"type": "message_id", "id": "msg-read"}
            yield {
                "type": "tool_use",
                "id": "tool-read-1",
                "name": "read_file",
                "input": {
                    "file_path": str(self.target),
                },
            }
            yield {"type": "usage", "input_tokens": 7, "output_tokens": 4}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return

        yield {"type": "message_id", "id": "msg-final"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


async def _run_with_permission_response(
    engine: CoreExecutionEngine,
    state: AgentState,
    *,
    grant: bool,
    decision: str | None = None,
):
    events: list[StreamEvent] = []
    async for event in engine._query_loop(state):
        events.append(event)
        if event.type == "permission_needed":
            tc = event.metadata["tool_call"]
            if decision is not None:
                engine.resolve_permission(decision, tc.name)
            else:
                engine.resolve_permission(grant, tc.name if grant else "")
    return events


async def _run_until_done(engine: CoreExecutionEngine, state: AgentState):
    return await _run_with_permission_response(engine, state, grant=True)


def _messages_include_tool_result(messages: list[dict], tool_use_id: str) -> bool:
    for message in messages:
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                return True
    return False


def _make_config(tmp_path, **overrides):
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
        **overrides,
    )


def _make_engine(tmp_path, client, **config_overrides):
    engine = CoreExecutionEngine(_make_config(tmp_path, **config_overrides))
    engine._build_client = lambda max_tokens=None: client
    return engine


def _make_state(text: str, **overrides):
    return AgentState(
        system_prompt="system",
        messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        **overrides,
    )


def test_permission_grant_allows_write_tool_to_execute(tmp_path):
    target = tmp_path / "created.txt"
    fake_client = _FakeClient(target)
    engine = _make_engine(tmp_path, fake_client)
    state = _make_state("create file")

    events = asyncio.run(_run_until_done(engine, state))

    assert target.read_text(encoding="utf-8") == "written by test"
    assert any(event.type == "permission_needed" for event in events)
    assert any(
        event.type == "tool_result"
        and event.metadata.get("tool_name") == "write_file"
        and not event.metadata.get("is_error")
        for event in events
    )
    assert events[-1].type == "done"
    assert not state.permission_state.is_tool_confirmed("write_file")
    assert not state.permission_state.is_path_confirmed(str(target))
    assert state.turn_count == 1


class _TwoWriteClient:
    def __init__(self, first: Path, second: Path):
        self.first = first
        self.second = second
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-1"}
            yield {
                "type": "tool_use",
                "id": "tool-write-1",
                "name": "write_file",
                "input": {"file_path": str(self.first), "content": "a"},
            }
            yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return
        if self.calls == 2:
            yield {"type": "message_id", "id": "msg-2"}
            yield {
                "type": "tool_use",
                "id": "tool-write-2",
                "name": "write_file",
                "input": {"file_path": str(self.second), "content": "b"},
            }
            yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return
        yield {"type": "message_id", "id": "msg-3"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


def test_once_write_grant_does_not_confirm_tool_or_other_paths(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    client = _TwoWriteClient(first, second)
    engine = _make_engine(tmp_path, client)
    state = _make_state("write")

    events = asyncio.run(
        _run_with_permission_response(engine, state, grant=True, decision="once")
    )

    assert first.read_text(encoding="utf-8") == "a"
    assert second.read_text(encoding="utf-8") == "b"
    assert sum(event.type == "permission_needed" for event in events) == 2
    assert not state.permission_state.is_tool_confirmed("write_file")


def test_always_write_grant_confirms_only_that_path(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    client = _TwoWriteClient(first, second)
    engine = _make_engine(tmp_path, client)
    state = _make_state("write")

    events = asyncio.run(
        _run_with_permission_response(engine, state, grant=True, decision="always")
    )

    assert state.permission_state.is_path_confirmed(str(first))
    assert not state.permission_state.is_tool_confirmed("write_file")
    assert sum(event.type == "permission_needed" for event in events) == 2


class _ShellCommandClient:
    def __init__(self, command: str):
        self.command = command
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-shell"}
            yield {
                "type": "tool_use",
                "id": "tool-shell",
                "name": "run_shell",
                "input": {"command": self.command},
            }
            yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return
        yield {"type": "message_id", "id": "msg-final"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


def test_run_shell_always_confirms_prefix_rule_not_tool(tmp_path):
    from xxcode.agent.permission_resolver import PermissionResolver
    from xxcode.tools import ToolCall
    from xxcode.tools.BashTool import BashTool

    state = _make_state("shell")

    PermissionResolver._persist_always_grant(
        state,
        ToolCall(
            id="tool-shell",
            name="run_shell",
            input={"command": "npm run build"},
        ),
        BashTool(),
    )

    assert not state.permission_state.is_tool_confirmed("run_shell")
    assert "Bash(npm run:*)" in state.permission_state.confirmed_command_rules
    assert state.permission_state.is_command_rule_confirmed("npm run test")


def test_run_shell_always_blocked_prefix_does_not_persist(tmp_path):
    from xxcode.agent.permission_resolver import PermissionResolver
    from xxcode.tools import ToolCall
    from xxcode.tools.BashTool import BashTool

    state = _make_state("shell")

    PermissionResolver._persist_always_grant(
        state,
        ToolCall(
            id="tool-shell",
            name="run_shell",
            input={"command": "bash -c echo ok"},
        ),
        BashTool(),
    )

    assert not state.permission_state.confirmed_command_rules
    assert not state.permission_state.is_tool_confirmed("run_shell")


def test_truncated_tool_use_executes_before_next_model_turn(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("hello from test", encoding="utf-8")
    config = _make_config(tmp_path, api_max_tokens=32)
    engine = CoreExecutionEngine(config)
    fake_client = _TruncatedToolUseClient(target)
    max_tokens_seen: list[int | None] = []
    engine._build_client = lambda max_tokens=None: (
        max_tokens_seen.append(max_tokens) or fake_client
    )
    state = _make_state("read file")

    events = asyncio.run(_run_until_done(engine, state))

    assert fake_client.calls == 2
    assert max_tokens_seen == [32, 32]
    assert any(
        event.type == "tool_result"
        and event.metadata.get("tool_name") == "read_file"
        and not event.metadata.get("is_error")
        for event in events
    )
    assert _messages_include_tool_result(
        fake_client.messages_by_call[1],
        "tool-read-1",
    )
    assert events[-1].type == "done"


def test_dangerous_permission_event_exposes_legacy_and_ui_risk_flags(tmp_path):
    fake_client = _DangerousShellClient()
    engine = _make_engine(tmp_path, fake_client)
    state = _make_state("run command")

    events = asyncio.run(
        _run_with_permission_response(engine, state, grant=False)
    )

    permission_events = [
        event for event in events if event.type == "permission_needed"
    ]
    assert len(permission_events) == 1
    metadata = permission_events[0].metadata
    assert metadata["risk"] == "high"
    assert metadata["dangerous"] is True
    assert metadata["tool_call"].name == "run_shell"
    assert any(
        event.type == "tool_result" and event.metadata.get("denied") is True
        for event in events
    )
    assert fake_client.calls == 2


def test_recalled_memories_are_visible_to_first_model_call(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "- [Preference](preference.md) - The user prefers concise answers.",
        encoding="utf-8",
    )
    fake_client = _CaptureFirstCallClient()
    engine = _make_engine(
        tmp_path,
        fake_client,
        auto_memory_directory=str(memory_dir),
    )
    state = _make_state(
        "answer with preference",
        last_query="answer with preference",
    )

    from xxcode.memory.models import MemoryType
    from xxcode.memory.recall import MemoryRecall

    preference_path = memory_dir / "preference.md"
    preference_path.write_text(
        "The user prefers concise answers.",
        encoding="utf-8",
    )

    async def _fake_recall(_state):
        return [
            MemoryRecall(
                filename="preference.md",
                file_path=preference_path,
                content=preference_path.read_text(encoding="utf-8"),
                memory_type=MemoryType.FEEDBACK,
            )
        ]

    engine._run_memory_recall = _fake_recall

    asyncio.run(_run_until_done(engine, state))

    first_messages = fake_client.messages_by_call[0]
    assert "Contents of" in str(first_messages)
    assert "user's auto-memory, persists across conversations" in str(first_messages)
    assert "The user prefers concise answers." in str(first_messages)
    assert "Memory (saved" in str(first_messages)
    assert "isMeta" not in str(first_messages)
    assert "xxcode_memory_context" not in str(first_messages)
    assert first_messages[-1]["role"] == "user"
    assert first_messages[-1]["content"][0]["text"] == "answer with preference"


def test_recalled_memories_append_after_tool_results_within_same_turn(tmp_path):
    from xxcode.memory.models import MemoryEntry
    from xxcode.memory.store import MemoryStore

    memory_dir = tmp_path / "memory"
    target = tmp_path / "source.txt"
    target.write_text("hello from test", encoding="utf-8")

    store = MemoryStore(memory_dir)
    store.save_entry(MemoryEntry(
        name="initial",
        description="Initial preference memory.",
        content="Initial memory body.",
        metadata={"type": "feedback"},
    ))
    store.save_entry(MemoryEntry(
        name="followup",
        description="Follow-up memory after reading files.",
        content="Follow-up memory body.",
        metadata={"type": "reference"},
    ))

    fake_client = _RecallAwareClient(target)
    engine = _make_engine(
        tmp_path,
        fake_client,
        auto_memory_directory=str(memory_dir),
    )
    state = _make_state(
        "read file and continue",
        last_query="read file and continue",
    )

    asyncio.run(_run_until_done(engine, state))

    assert fake_client.stream_calls == 2
    assert fake_client.complete_calls == 2
    second_recall_prompt = fake_client.recall_messages[1][0]["content"]
    assert "Already shown" in second_recall_prompt
    assert "initial.md" in second_recall_prompt

    second_messages = fake_client.messages_by_stream_call[1]
    combined_text = "\n".join(
        block["text"]
        for message in second_messages
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert combined_text.count("Initial memory body.") == 1
    assert combined_text.count("Follow-up memory body.") == 1
    assert _messages_include_tool_result(second_messages, "tool-read-1")


def test_tool_search_like_inputs_do_not_trigger_followup_recall():
    assert CoreExecutionEngine._is_read_like_tool(
        "tool_search",
        tool=None,
        raw_input={"query": "find notebook tools"},
    ) is False
