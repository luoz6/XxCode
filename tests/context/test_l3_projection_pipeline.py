import pytest

from xxcode.agent.state import AgentState
from xxcode.config import Config
from xxcode.context.auto import AUTOCOMPACT_BUFFER_TOKENS, MAX_OUTPUT_TOKENS_FOR_SUMMARY
from xxcode.context.collapse import (
    _DEFAULT_COLLAPSE_THRESHOLD_TOKENS,
    CollapsedRegion,
    get_l3_collapse_threshold,
)
from xxcode.context.pipeline import ContextPipeline


def test_l3_threshold_is_bounded_before_l4_for_200k_window():
    context_limit = 200_000
    soft_limit = int(context_limit * 0.85)
    l4_threshold = context_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY - AUTOCOMPACT_BUFFER_TOKENS

    threshold = get_l3_collapse_threshold(context_limit=context_limit, soft_limit=soft_limit)

    assert threshold == min(_DEFAULT_COLLAPSE_THRESHOLD_TOKENS, soft_limit, l4_threshold - 1)
    assert threshold <= soft_limit
    assert threshold < l4_threshold


def test_l3_threshold_keeps_existing_90k_upper_bound():
    assert get_l3_collapse_threshold(context_limit=200_000, soft_limit=170_000) == 90_000


def _make_pipeline(tmp_path):
    return ContextPipeline(
        Config(
            api_key="key",
            api_base_url="https://example.test",
            api_model="deepseek-chat",
            cwd=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    )


def _large_messages(count=12):
    messages = []
    for idx in range(count):
        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": f"user-{idx} " + "x" * 4000}],
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"assistant-{idx} " + "y" * 4000}],
            }
        )
    return messages


@pytest.mark.asyncio
async def test_pipeline_l3_returns_regions_without_mutating_messages(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    messages = _large_messages()
    original = [dict(message) for message in messages]

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=20_000,
        threshold=0.5,
        state=AgentState(system_prompt="system"),
        allow_autocompact=False,
    )

    assert messages == original
    assert stats.level_reached >= 3
    assert hasattr(stats, "collapsed_regions")
    assert stats.collapsed_regions
    assert compressed is not messages
    assert compressed == original


@pytest.mark.asyncio
async def test_l3_active_suppresses_l4(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    called = {"auto": False}

    async def _fake_autocompact(self, current, system_prompt):
        called["auto"] = True
        return "summary"

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fake_autocompact)

    _compressed, stats = await pipeline.compress(
        _large_messages(),
        current_tokens=None,
        context_limit=20_000,
        threshold=0.2,
        state=AgentState(system_prompt="system"),
        allow_autocompact=True,
    )

    assert stats.level_reached == 3
    assert stats.auto_triggered is False
    assert called["auto"] is False


@pytest.mark.asyncio
async def test_existing_l3_regions_report_projection_savings_when_l4_disallowed(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    messages = _large_messages()
    existing_regions = [
        CollapsedRegion(
            start_idx=0,
            end_idx=14,
            summary="[Earlier conversation -- summarized]",
        )
    ]

    compressed, stats = await pipeline.compress(
        messages,
        current_tokens=None,
        context_limit=20_000,
        threshold=0.5,
        state=AgentState(system_prompt="system"),
        existing_l3_regions=existing_regions,
        allow_autocompact=False,
    )

    assert compressed == messages
    assert stats.collapsed_regions == existing_regions
    assert stats.collapse_count > 0
    assert stats.collapse_tokens_freed > 0
    assert stats.auto_triggered is False
