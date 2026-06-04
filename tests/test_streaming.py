"""Tests for the lightweight SSE parser."""

import asyncio

from xxcode.api.streaming import parse_sse_stream


async def _collect(lines):
    async def _iter():
        for line in lines:
            yield line

    return [event async for event in parse_sse_stream(_iter())]


def test_parse_sse_stream_yields_valid_json_data_lines():
    result = asyncio.run(_collect([
        ": keep-alive",
        "",
        'data: {"type": "text_delta", "text": "hello"}',
        'data: {"type": "usage", "input_tokens": 2}',
        "data: [DONE]",
        'data: {"type": "ignored"}',
    ]))

    assert result == [
        {"type": "text_delta", "text": "hello"},
        {"type": "usage", "input_tokens": 2},
    ]


def test_parse_sse_stream_skips_invalid_json():
    result = asyncio.run(_collect([
        "data: not-json",
        'data: {"type": "ok"}',
    ]))

    assert result == [{"type": "ok"}]
