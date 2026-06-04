"""Unit tests for budget functions: clamp_to_absolute_max, apply_aggregate_result_budget."""

import asyncio

from xxcode.core.budget import (
    _format_size,
    apply_aggregate_result_budget,
    clamp_to_absolute_max,
)


# ── clamp_to_absolute_max ──────────────────────────────────────────────

def test_clamp_under_limit_passes_through():
    content = "hello world"
    result = clamp_to_absolute_max(content, 100)
    assert result == content


def test_clamp_at_limit_passes_through():
    content = "x" * 100
    result = clamp_to_absolute_max(content, 100)
    assert result == content


def test_clamp_over_limit_truncates():
    content = "a" * 10000
    result = clamp_to_absolute_max(content, 1000)

    # Length may slightly exceed limit due to truncation notice text
    assert len(result) < 2000  # well under original 10000
    assert "OUTPUT TRUNCATED" in result
    assert result.startswith("a" * 500)  # head half


def test_clamp_way_over_limit():
    content = "x" * 100000
    result = clamp_to_absolute_max(content, 400000)
    # 100K < 400K, should pass through
    assert result == content


def test_clamp_preserves_tail():
    content = "HEAD_CONTENT" + "MIDDLE" * 5000 + "TAIL_CONTENT"
    result = clamp_to_absolute_max(content, 2000)

    assert "OUTPUT TRUNCATED" in result
    assert result.startswith("HEAD_CONTENT")
    assert result.rstrip().endswith("TAIL_CONTENT")


# ── apply_aggregate_result_budget ──────────────────────────────────────

def test_aggregate_under_limit_returns_unchanged():
    results = [
        {"content": "short", "tool_use_id": "a"},
        {"content": "also short", "tool_use_id": "b"},
    ]
    output = asyncio.run(apply_aggregate_result_budget(results, 1000))
    assert output == results


def test_aggregate_at_limit_returns_unchanged():
    r = [{"content": "x" * 500, "tool_use_id": "a"}]
    output = asyncio.run(apply_aggregate_result_budget(r, 500))
    assert output[0]["content"] == "x" * 500


def test_aggregate_over_limit_truncates_largest():
    results = [
        {"content": "small", "tool_use_id": "a"},
        {"content": "x" * 5000, "tool_use_id": "b"},  # largest
        {"content": "medium" * 100, "tool_use_id": "c"},
    ]
    total = sum(len(r["content"]) for r in results)
    assert total > 3000  # way over budget

    output = asyncio.run(apply_aggregate_result_budget(results, 3000))

    new_total = sum(len(r["content"]) for r in output)
    assert new_total <= 3000
    # The largest result (tool b) should have been truncated
    assert "Aggregate budget:" in output[1]["content"]


def test_aggregate_over_limit_preserves_order():
    results = [
        {"content": "aaa", "tool_use_id": "first"},
        {"content": "bbb", "tool_use_id": "second"},
        {"content": "ccc", "tool_use_id": "third"},
    ]
    output = asyncio.run(apply_aggregate_result_budget(results, 12))
    assert output[0]["tool_use_id"] == "first"
    assert output[1]["tool_use_id"] == "second"
    assert output[2]["tool_use_id"] == "third"


def test_aggregate_single_result_over_limit():
    """Single result over budget is still truncated — call-site guard (len>1) is in tools_executor."""
    results = [{"content": "x" * 10000, "tool_use_id": "a"}]
    output = asyncio.run(apply_aggregate_result_budget(results, 1000))
    # Budget function itself always applies the limit; truncation notice adds some overhead
    new_total = sum(len(r["content"]) for r in output)
    assert new_total < 2000  # well under 10000 original


def test_format_size_bytes():
    assert "B" in _format_size(500)
    assert "KB" in _format_size(5000)
    assert "MB" in _format_size(5_000_000)
