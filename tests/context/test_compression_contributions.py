import pytest

import xxcode.context.pipeline as pipeline_module

from xxcode.agent.state import AgentState
from xxcode.config import Config
from xxcode.context.pipeline import ContextPipeline
from xxcode.context.tokens import token_count_with_estimation


def _make_pipeline(tmp_path):
    config = Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
    )
    return ContextPipeline(config)


def _tool_result_message(text: str, tool_use_id: str = "tool-1") -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
            }
        ],
    }


@pytest.mark.asyncio
async def test_l1_reports_token_and_character_contributions(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    noisy = (
        "Collecting demo-package\n"
        "Downloading demo-package\n"
        "Requirement already satisfied: demo-package\n"
        "Successfully installed demo-package\n\n"
        + ("Collecting demo-package\nDownloading demo-package\n" * 120)
    )

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        [_tool_result_message(noisy)],
        current_tokens=None,
        context_limit=200,
        threshold=0.5,
    )

    assert compressed != [_tool_result_message(noisy)]
    assert stats.snip_removed > 0
    assert stats.snip_tokens_freed > 0
    assert stats.micro_tokens_freed == 0
    assert stats.collapse_tokens_freed == 0
    assert stats.auto_tokens_freed == 0
    assert stats.tokens_before - stats.tokens_after == stats.snip_tokens_freed


@pytest.mark.asyncio
async def test_l1_no_noise_keeps_zero_contribution(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    clean = "\n".join(["plain output"] * 400)

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    _compressed, stats = await pipeline.compress(
        [_tool_result_message(clean)],
        current_tokens=None,
        context_limit=200,
        threshold=0.2,
    )

    assert stats.snip_removed == 0
    assert stats.snip_tokens_freed == 0
    assert stats.level_reached >= 1


def _compressible_round(tool_name: str, tool_use_id: str, text: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {"path": "/fake/file.txt"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": text,
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_l2_reports_exact_cleared_block_count(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    messages.extend(_compressible_round("read_file", "tool-1", "A" * 1800))
    messages.extend(_compressible_round("run_shell", "tool-2", "B" * 1800))
    messages.extend(_compressible_round("grep_search", "tool-3", "C" * 1800))

    monkeypatch.setattr(pipeline_module, "collapse_messages", lambda current, keep_recent=5: current)
    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=200,
        threshold=0.4,
    )

    assert stats.micro_cleared == 2
    assert stats.micro_tokens_freed > 0
    assert stats.micro_truncated == 2

    preserved_contents = [
        block["content"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "tool_result"
    ]
    assert "C" * 1800 in preserved_contents


def _exchange(turn_id: int, text_size: int = 500) -> list[dict]:
    text = f"turn-{turn_id}-" + ("x" * text_size)
    return [
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {"role": "assistant", "content": [{"type": "text", "text": text[::-1]}]},
    ]


@pytest.mark.asyncio
async def test_l3_reports_net_message_reduction_and_token_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(8):
        messages.extend(_exchange(turn_id))

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=500,
        threshold=0.4,
    )

    assert stats.collapse_tokens_freed > 0
    # 16 messages total, and with the current role-alternation partitioning
    # each message forms its own exchange. keep_recent=5 preserves the newest
    # 5 messages; the older 11 messages collapse to 1 summary.
    # Net reduction = 16 - (1 + 5) = 10.
    assert stats.collapse_count == 10
    collapsed_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]
    assert any(text.startswith("[Earlier conversation") for text in collapsed_texts)


@pytest.mark.asyncio
async def test_l4_success_reports_budget_and_token_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    seen: dict[str, int] = {}

    async def _fake_autocompact(self, current, system_prompt):
        seen["post_l3_tokens"] = token_count_with_estimation(current)
        return "condensed summary"

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fake_autocompact)

    state = AgentState(system_prompt="system")
    state.task_budget_remaining = 50_000

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
        state=state,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is True
    assert stats.auto_tokens_freed > 0
    assert any("[Conversation summary]" in text for text in summary_texts)
    assert state.task_budget_remaining == 50_000 - seen["post_l3_tokens"]


@pytest.mark.asyncio
async def test_l4_suppressed_keeps_zero_auto_contribution(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is False
    assert stats.auto_tokens_freed == 0
    assert all("[Conversation summary]" not in text for text in summary_texts)


@pytest.mark.asyncio
async def test_l4_failure_still_marks_attempt_but_no_token_gain(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    async def _fail_autocompact(self, current, system_prompt):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fail_autocompact)

    state = AgentState(system_prompt="system")
    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
        state=state,
    )

    summary_texts = [
        block["text"]
        for msg in compressed
        for block in msg.get("content", [])
        if block.get("type") == "text"
    ]

    assert stats.auto_triggered is True
    assert stats.auto_tokens_freed == 0
    assert all("[Conversation summary]" not in text for text in summary_texts)
    assert state.consecutive_autocompact_failures == 1


@pytest.mark.asyncio
async def test_end_to_end_contributions_telescope_to_total_delta(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    messages: list[dict] = []
    messages.append(
        _tool_result_message(
            "Collecting demo\nDownloading demo\nSuccessfully installed demo\n" + ("x" * 1200),
            tool_use_id="noise-1",
        )
    )
    messages.extend(_compressible_round("read_file", "tool-1", "A" * 1800))
    messages.extend(_compressible_round("run_shell", "tool-2", "B" * 1800))
    for turn_id in range(12):
        messages.extend(_exchange(turn_id, text_size=1200))

    async def _fake_autocompact(self, current, system_prompt):
        return "condensed summary"

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fake_autocompact)

    _compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=1000,
        threshold=0.1,
    )

    per_level = (
        stats.snip_tokens_freed
        + stats.micro_tokens_freed
        + stats.collapse_tokens_freed
        + stats.auto_tokens_freed
    )

    positive_levels = sum(
        value > 0
        for value in (
            stats.snip_tokens_freed,
            stats.micro_tokens_freed,
            stats.collapse_tokens_freed,
            stats.auto_tokens_freed,
        )
    )

    assert stats.tokens_before > stats.tokens_after
    assert positive_levels >= 2
    assert per_level == stats.tokens_before - stats.tokens_after
