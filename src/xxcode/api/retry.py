"""Retry strategy with exponential backoff and jitter."""

import asyncio
import random
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0
    jitter: bool = True

    # HTTP status codes that trigger a retry
    retryable_statuses: tuple[int, ...] = (429,)

    # Status codes that are retryable ONLY for overload, not for user errors
    conditional_retry_statuses: tuple[int, ...] = (503, 529)

    # Error keywords that should NOT be retried (permanent errors)
    non_retryable_keywords: tuple[str, ...] = (
        "model_not_found", "invalid_request_error", "invalid_api_key",
        "authentication", "authorization", "not_found", "permission",
    )


async def retry_with_backoff(
    stream_fn: Callable,
    build_request: Callable,
    config: RetryConfig,
) -> AsyncGenerator[dict, None]:
    """Execute a streaming request with exponential backoff retry.

    Args:
        stream_fn: Async function that takes (url, headers, body) and yields events.
        build_request: Function that returns (url, headers, body) — called fresh on each retry.
        config: Retry configuration.

    Yields:
        Events from the stream. On retryable error, retries with backoff.
        On non-retryable error, yields an error event and stops.
    """
    last_error: str | None = None

    for attempt in range(config.max_retries + 1):
        try:
            url, headers, body = build_request()
            async for event in stream_fn(url, headers, body):
                if event.get("type") == "error":
                    yield event
                    return
                yield event
            return  # Success

        except _RetryableError as e:
            last_error = str(e)
            if attempt < config.max_retries:
                delay = config.base_delay * (config.backoff_factor ** attempt)
                delay = min(delay, config.max_delay)
                if config.jitter:
                    delay *= 0.5 + random.random()
                await asyncio.sleep(delay)

    yield {
        "type": "error",
        "message": f"Request failed after {config.max_retries + 1} attempts. Last error: {last_error}",
    }


class _RetryableError(Exception):
    """Internal marker for errors that should trigger a retry."""
    pass
