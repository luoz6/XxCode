"""SSE streaming parser — lightweight, kept for future non-Anthropic backends."""

from collections.abc import AsyncGenerator


async def parse_sse_stream(
    line_iterator: AsyncGenerator[str, None],
) -> AsyncGenerator[dict, None]:
    """Parse Server-Sent Events from an async line iterator.

    Yields parsed JSON objects from data lines.
    """
    import json

    async for line in line_iterator:
        line = line.strip()
        if not line:
            continue
        if line.startswith(":"):
            continue  # SSE comment
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError:
                continue
