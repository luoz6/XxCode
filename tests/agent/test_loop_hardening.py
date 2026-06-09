import asyncio

from xxcode.agent.loop import CoreExecutionEngine, _repair_orphan_tools
from xxcode.agent.continue_reasons import ContinueReason
from xxcode.agent.output_recovery import ESCALATED_MAX_TOKENS
from xxcode.agent.ptl_recovery import PTLRecoveryManager
from xxcode.agent.state import AgentState
from xxcode.config import Config
from xxcode.context.collapse import CollapsedRegion
from xxcode.context.micro import CacheEdit
from xxcode.context.pipeline import CompressionStats, ContextPipeline
from xxcode.tools.file_edit.types import FileStateEntry


class _TruncateThenFinishClient:
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-1"}
            yield {"type": "text_delta", "text": "partial"}
            yield {"type": "usage", "input_tokens": 5, "output_tokens": 6}
            yield {"type": "stop_reason", "stop_reason": "max_tokens"}
            return
        yield {"type": "message_id", "id": "msg-2"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 7, "output_tokens": 8}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


async def _collect_events(engine, state):
    return [event async for event in engine._query_loop(state)]


def _make_config(tmp_path, **overrides):
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
        **overrides,
    )


def _make_state(text=None, *, messages=None, **overrides):
    if messages is None:
        if text is None:
            raise ValueError("text is required when messages are not provided")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ]
    return AgentState(
        system_prompt="system",
        messages=messages,
        **overrides,
    )


async def test_output_token_escalation_is_used_for_next_client(tmp_path):
    config = _make_config(
        tmp_path,
        api_max_tokens=32,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = CoreExecutionEngine(config)
    client = _TruncateThenFinishClient()
    max_tokens_seen = []

    def _build_client(max_tokens=None, **kwargs):
        max_tokens_seen.append(max_tokens)
        return client

    engine._build_client = _build_client
    state = _make_state("hello")

    events = await _collect_events(engine, state)

    assert [event.type for event in events][-1] == "done"
    assert max_tokens_seen[:2] == [32, ESCALATED_MAX_TOKENS]


async def test_ptl_collapse_drain_shortens_state_messages(tmp_path):
    config = _make_config(tmp_path)
    state = _make_state(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "x" * 100_000}]},
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "y" * 100_000}]},
            {"role": "user", "content": [{"type": "text", "text": "now"}]},
        ],
    )
    before = len(str(state.messages))
    manager = PTLRecoveryManager(config=config, regions=[])

    action, event = await manager.recover(state, "prompt is too long")

    assert action == "retry"
    assert event is None
    assert len(str(state.messages)) < before
    assert state.last_continue_reason in {
        ContinueReason.COLLAPSE_DRAIN_RETRY,
        ContinueReason.REACTIVE_COMPACT_RETRY,
    }


def _tool_pairing_ids(messages):
    uses = set()
    results = set()
    for message in messages:
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                uses.add(block.get("id", ""))
            elif block.get("type") == "tool_result":
                results.add(block.get("tool_use_id", ""))
    return uses, results


async def test_ptl_collapse_drain_preserves_tool_pairings(tmp_path):
    config = _make_config(tmp_path)
    state = _make_state(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "reading"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "read_file",
                        "input": {"file_path": "a.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "a" * 100_000,
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "reading again"},
                    {
                        "type": "tool_use",
                        "id": "tool-2",
                        "name": "read_file",
                        "input": {"file_path": "b.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-2",
                        "content": "b" * 100_000,
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "now"}]},
        ],
    )
    manager = PTLRecoveryManager(config=config, regions=[])

    action, event = await manager.recover(state, "prompt is too long")

    assert action == "retry"
    assert event is None
    uses, results = _tool_pairing_ids(state.messages)
    assert uses == results


async def test_ptl_drain_clears_stale_runtime_compression_state(tmp_path):
    config = _make_config(tmp_path)
    engine = CoreExecutionEngine(config)
    state = _make_state(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "start " + "x" * 100_000}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply " + "y" * 100_000}]},
            {"role": "user", "content": [{"type": "text", "text": "now"}]},
        ],
    )
    state.cache_breakpoints = {1}
    engine._l3_regions = [object()]
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))

    manager = PTLRecoveryManager(config=config, regions=engine._l3_regions)
    action, _event = await manager.recover(state, "prompt is too long")
    if action == "retry":
        engine._clear_runtime_compression_state_after_history_replace(state)

    assert engine._l3_regions == []
    assert engine._cache_edit_state.pending == []
    assert state.cache_breakpoints == set()


class _CaptureMessagesClient:
    def __init__(self):
        self.messages = None

    async def stream_chat(self, system_prompt, messages, tools, **kwargs):
        self.messages = messages
        yield {"type": "message_id", "id": "msg-capture"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


async def test_l3_regions_are_projected_for_request_without_rewriting_state(tmp_path):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
        context_compress_threshold=0.0,
    )
    engine = CoreExecutionEngine(config)
    client = _CaptureMessagesClient()
    engine._build_client = lambda max_tokens=None, **kwargs: client
    original_messages = []
    for idx in range(8):
        original_messages.append(
            {"role": "user", "content": [{"type": "text", "text": f"user-{idx} " + "x" * 4000}]}
        )
        original_messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": f"assistant-{idx} " + "y" * 4000}]}
        )
    state = _make_state(messages=[dict(message) for message in original_messages])

    events = await _collect_events(engine, state)

    assert [event.type for event in events][-1] == "done"
    assert engine._l3_regions
    assert state.messages[: len(original_messages)] == original_messages
    projected_texts = [
        block["text"]
        for message in client.messages
        for block in message.get("content", [])
        if block.get("type") == "text"
    ]
    assert any(text.startswith("[Earlier conversation") for text in projected_texts)
    assert len(client.messages) < len(original_messages)


async def test_existing_l3_projection_prevents_unneeded_compression_pass(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
        context_compress_threshold=0.01,
    )
    engine = CoreExecutionEngine(config)
    client = _CaptureMessagesClient()
    engine._build_client = lambda max_tokens=None, **kwargs: client
    state = _make_state(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "old " + "x" * 20_000}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply " + "y" * 20_000}]},
        ]
    )
    engine._l3_regions = [
        CollapsedRegion(
            start_idx=0,
            end_idx=2,
            summary="[Earlier conversation -- summarized]",
        )
    ]

    async def _unexpected_compress(*args, **kwargs):
        raise AssertionError("compression should use projected tokens for the entry check")

    monkeypatch.setattr(ContextPipeline, "compress", _unexpected_compress)

    events = await _collect_events(engine, state)

    assert [event.type for event in events][-1] == "done"
    assert client.messages == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "[Earlier conversation -- summarized]"}],
        }
    ]


async def test_l4_success_restores_recent_read_files(tmp_path, monkeypatch):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
        context_compress_threshold=0.0,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _CaptureMessagesClient()
    state = _make_state("trigger compression")
    state.read_file_state = {
        "/old.py": FileStateEntry(content="old content", timestamp=1.0),
        "/new.py": FileStateEntry(content="new content", timestamp=2.0),
    }

    async def _fake_compress(
        self,
        messages,
        current_tokens=None,
        system_prompt="",
        context_limit=200_000,
        threshold=None,
        state=None,
        existing_l3_regions=None,
        allow_autocompact=True,
    ):
        stats = CompressionStats(level_reached=4, auto_triggered=True)
        return [
            {
                "role": "user",
                "content": [{"type": "text", "text": "[Conversation summary]\nsummary"}],
            }
        ], stats

    monkeypatch.setattr(ContextPipeline, "compress", _fake_compress)

    events = await _collect_events(engine, state)

    assert [event.type for event in events][-1] == "done"
    recovery_texts = [
        block["text"]
        for message in state.messages
        for block in message.get("content", [])
        if block.get("type") == "text"
    ]
    assert any("[System: Post-compact memory restoration]" in text for text in recovery_texts)
    assert any("/new.py" in text and "new content" in text for text in recovery_texts)


class _NeverEndingToolClient:
    async def stream_chat(self, system_prompt, messages, tools):
        yield {"type": "message_id", "id": "msg-loop"}
        yield {
            "type": "tool_use",
            "id": "tool-read",
            "name": "read_file",
            "input": {"file_path": "missing.txt"},
        }
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "tool_use"}


async def test_parent_turn_limit_uses_config_value(tmp_path):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
        max_parent_turns=1,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _NeverEndingToolClient()
    state = _make_state("loop")

    events = await _collect_events(engine, state)

    error_text = "\n".join(event.content for event in events if event.type == "error")
    assert "Reached maximum turns (1)" in error_text


class _FatalErrorClient:
    async def stream_chat(self, system_prompt, messages, tools):
        yield {"type": "error", "message": "HTTP 503: model not found"}


class _FatalExceptionClient:
    async def stream_chat(self, system_prompt, messages, tools):
        raise RuntimeError("HTTP 503: model not found")
        yield


async def test_fatal_stream_error_is_visible(tmp_path):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _FatalErrorClient()
    state = _make_state("hello")

    events = await _collect_events(engine, state)

    assert any(event.type == "error" and "model not found" in event.content for event in events)


async def test_fatal_api_exception_is_visible_without_retry_label(tmp_path):
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _FatalExceptionClient()
    state = _make_state("hello")

    events = await _collect_events(engine, state)

    error_text = "\n".join(event.content for event in events if event.type == "error")
    assert "model not found" in error_text
    assert "recovered" not in error_text


class _FastFinishClient:
    def __init__(self):
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        yield {"type": "message_id", "id": "msg-fast"}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


async def test_delayed_memory_recall_does_not_block_first_request(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    config = _make_config(
        tmp_path,
        auto_memory_enabled=True,
        auto_memory_directory=str(memory_dir),
        mcp_enabled=False,
        skills_enabled=False,
        memory_recall_prefetch_timeout_seconds=0.01,
    )
    engine = CoreExecutionEngine(config)
    client = _FastFinishClient()
    engine._build_client = lambda max_tokens=None, **kwargs: client

    async def _slow_recall(state):
        await asyncio.sleep(1)
        return []

    engine._run_memory_recall = _slow_recall
    state = _make_state("remember", last_query="remember")

    await asyncio.wait_for(
        _collect_events(engine, state),
        timeout=0.5,
    )

    assert client.calls == 1


async def test_delayed_memory_recall_task_is_cleaned_up_on_loop_exit(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    config = _make_config(
        tmp_path,
        auto_memory_enabled=True,
        auto_memory_directory=str(memory_dir),
        mcp_enabled=False,
        skills_enabled=False,
        memory_recall_prefetch_timeout_seconds=0.01,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _FastFinishClient()
    recall_started = asyncio.Event()
    recall_cancelled = asyncio.Event()

    async def _slow_recall(state):
        recall_started.set()
        try:
            await asyncio.sleep(10)
            return []
        except asyncio.CancelledError:
            recall_cancelled.set()
            raise

    engine._run_memory_recall = _slow_recall
    state = _make_state("remember", last_query="remember")

    await _collect_events(engine, state)

    assert recall_started.is_set()
    assert recall_cancelled.is_set()


# ── Fix 1: Orphan tool repair unit tests ────────────────────────────────


def test_repair_orphan_tools_creates_synthetic_result():
    """Orphan tool_use gets a synthetic tool_result in a new user message."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}}
            ],
        },
    ]
    repaired = _repair_orphan_tools(messages)
    assert len(repaired) == 2
    assert repaired[1]["role"] == "user"
    assert repaired[1]["content"][0]["type"] == "tool_result"
    assert repaired[1]["content"][0]["tool_use_id"] == "t1"
    assert "interrupted" in repaired[1]["content"][0]["content"]


def test_repair_orphan_tools_drops_orphan_result():
    """Orphan tool_result (no matching tool_use) is dropped."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "orphan"}
            ],
        },
    ]
    repaired = _repair_orphan_tools(messages)
    assert not any(
        b.get("tool_use_id") == "ghost"
        for msg in repaired
        for b in msg.get("content", [])
        if isinstance(b, dict)
    )


def test_repair_orphan_tools_preserves_valid_pairs():
    """Valid tool_use + tool_result pairs pass through unchanged."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
            ],
        },
    ]
    repaired = _repair_orphan_tools(messages)
    uses, results = _tool_pairing_ids(repaired)
    assert "t1" in uses
    assert "t1" in results
    assert uses == results


# ── Fix 1: PTL collapse drain orphan repair integration test ────────


async def test_ptl_collapse_drain_repairs_orphan_tool_use(tmp_path, monkeypatch):
    """After PTL collapse drain drops paired results, orphan tool_use
    blocks should gain synthetic tool_results rather than be deleted.

    Uses monkeypatch to return messages where collapse has already
    removed the matching tool_result, leaving an orphan tool_use."""
    config = _make_config(tmp_path)

    # Simulated post-collapse messages: tool_use "orphan-1" has no
    # matching tool_result (it was collapsed away).
    collapsed_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[Earlier conversation — summarized]\nTurn 1 collapsed for context efficiency."}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "orphan-1", "name": "read_file", "input": {"file_path": "a.py"}},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "now"}]},
    ]

    def _fake_collapse(msgs, keep_recent=5):
        return list(collapsed_messages)

    monkeypatch.setattr(
        "xxcode.context.collapse.collapse_messages",
        _fake_collapse,
    )

    state = _make_state(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "start " + "X" * 50000}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok " + "Y" * 50000}]},
            {"role": "user", "content": [{"type": "text", "text": "now " + "Z" * 50000}]},
        ],
    )
    manager = PTLRecoveryManager(config=config, regions=[])

    action, event = await manager.recover(state, "prompt is too long")

    assert action == "retry", f"Expected retry, got {action}"
    assert event is None
    uses, results = _tool_pairing_ids(state.messages)
    assert "orphan-1" in uses, (
        "Orphan tool_use must survive collapse repair "
        "(should get synthetic result, not be deleted)"
    )
    assert "orphan-1" in results, (
        "Orphan tool_use must receive a synthetic tool_result"
    )
    assert uses == results


# ── PTL coverage gap test ────────────────────────────────────────────


async def _async_false():
    return False


async def test_ptl_both_collapse_drain_and_reactive_compact_fail(tmp_path):
    """When both collapse drain and reactive compact fail, PTL recovery
    returns 'fail' with an error event."""
    config = _make_config(tmp_path)
    state = _make_state("short")
    manager = PTLRecoveryManager(config=config, regions=[])
    manager.try_collapse_drain = lambda s: _async_false()
    manager.reactive_compact = lambda s: _async_false()

    action, event = await manager.recover(state, "prompt is too long")

    assert action == "fail"
    assert event is not None
    assert event.type == "error"
    assert "unable to reduce" in event.content


# ── Fix 2: Non-fatal error retry test clients ────────────────────────


class _ErrorThenSuccessNoToolClient:
    """First call: non-fatal stream error with no tool calls.
    Second call: successful text response."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "error", "message": "API overloaded - please retry"}
            return
        yield {"type": "message_id", "id": "msg-ok"}
        yield {"type": "text_delta", "text": "recovered successfully"}
        yield {"type": "usage", "input_tokens": 2, "output_tokens": 3}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _RepeatingErrorToolClient:
    """Yields non-fatal errors with tool calls on each invocation.
    Errors repeat enough times to exhaust the retry budget."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        yield {
            "type": "tool_use",
            "id": f"tool-{self.calls}",
            "name": "read_file",
            "input": {"file_path": "test.txt"},
        }
        yield {"type": "error", "message": f"API degraded - call {self.calls}"}


async def test_non_fatal_stream_error_without_tool_calls_retries_until_success(tmp_path):
    """A non-fatal stream error with retry budget remaining must retry
    the request rather than terminating. The second call should succeed."""
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = CoreExecutionEngine(config)
    client = _ErrorThenSuccessNoToolClient()
    engine._build_client = lambda max_tokens=None, **kwargs: client
    state = _make_state("hello")

    events = await _collect_events(engine, state)

    assert client.calls == 2, (
        f"Expected 2 API calls (error + retry), got {client.calls}"
    )
    assert any(e.type == "error" for e in events), "Error should be visible"
    assert events[-1].type == "done"
    texts = " ".join(e.content for e in events if e.type == "text")
    assert "recovered successfully" in texts, (
        "Second (retry) call's response should appear in output"
    )


async def test_non_fatal_stream_error_exhausts_retries_for_tool_turns(tmp_path):
    """After MAX_API_ERROR_RETRIES consecutive stream errors (with tool calls
    to keep the loop alive), the engine must terminate with the retry-exhausted
    error message."""
    config = _make_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = CoreExecutionEngine(config)
    engine._build_client = lambda max_tokens=None, **kwargs: _RepeatingErrorToolClient()
    state = _make_state("test")

    events = await _collect_events(engine, state)

    error_text = " ".join(e.content for e in events if e.type == "error")
    assert "after 3 retries" in error_text
