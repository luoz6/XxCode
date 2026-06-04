"""L4: Autocompact — nuclear option: spawn a sub-agent to summarize everything.

This is the most expensive and aggressive compression level. It calls the API
with a summarization prompt and replaces the conversation with a condensed
summary + only the most recent exchanges.

Fault-tolerance mechanisms:
  - PTL (Prompt-Too-Long) retry: when the API rejects the summarization request
    because the input is too long, we drop the oldest conversation round and retry.
  - Circuit breaker: after MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES consecutive
    failures, autocompact is permanently disabled for the session.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Awaitable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Fault-tolerance constants ─────────────────────────────────────────

# Global circuit breaker: after this many consecutive failures, autocompact
# is permanently disabled for the current session.
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# Local retry: when the API returns a PTL error, truncate the head and
# retry up to this many times before giving up.
MAX_PTL_RETRIES = 3

# Strict token budget for autocompact threshold calculation.
#   context_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY = effective input window
#   effective_window - AUTOCOMPACT_BUFFER_TOKENS      = trigger threshold
AUTOCOMPACT_BUFFER_TOKENS = 13000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20000

# Post-compact memory restoration limits.
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5000

# Keywords in API error messages that signal a Prompt-Too-Long condition.
_PTL_ERROR_KEYWORDS = ("too_long", "token limit", "context length", "prompt is too long",
                        "maximum context", "exceeds the maximum", "max_tokens",
                        "input length", "reduce the length")


@dataclass
class AutoCompactResult:
    """Result of an L4 autocompact pass."""

    summary: str
    original_tokens: int
    summary_tokens: int
    success: bool = True
    error: str | None = None


async def autocompact(
    messages: list[dict],
    system_prompt: str,
    *,
    api_key: str,
    api_base_url: str,
    api_model: str,
    max_summary_tokens: int = 500,
) -> AutoCompactResult:
    """Generate a full-conversation summary via a lightweight API call.

    This is the nuclear compression option — it replaces the entire
    conversation history with a structured summary.

    Args:
        messages: Full conversation to summarize.
        system_prompt: Original system prompt (for context about task).
        api_key: API key.
        api_base_url: API base URL.
        api_model: Model to use for summarization (use fast/cheap model).
        max_summary_tokens: Max tokens for the summary response.

    Returns:
        AutoCompactResult with the summary text and stats.
    """
    from ..api.retry import RetryConfig

    prompt = _build_summary_prompt(messages)

    compact_system = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. "
        "Tool calls will be REJECTED and will waste your only turn.\n\n"
        "You are a summarization agent. You must compress the conversation "
        "using a two-stage process:\n"
        "1. <analysis>: Write a chronological scratchpad of the user's "
        "intent, methods tried, and errors.\n"
        "2. <summary>: Provide the final summary. It MUST include these "
        "sections:\n"
        "   - Primary Request\n"
        "   - Files and Code (include absolute paths)\n"
        "   - Errors and Fixes\n"
        "   - Current Work\n"
        "   - Pending Tasks"
    )

    from ..api.client import create_llm_client

    client = create_llm_client(
        api_key=api_key,
        base_url=api_base_url,
        model=api_model,
        max_tokens=max_summary_tokens,
        retry_config=RetryConfig(),
    )

    try:
        full_text = ""
        async for event in client.stream_chat(
            system_prompt=compact_system,
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            tools=[],
        ):
            if event["type"] == "text_delta":
                full_text += event["text"]
            elif event["type"] == "error":
                msg = event.get("message", "Unknown")
                logger.warning("L4 autocompact API error: %s", msg)
                return AutoCompactResult(
                    summary="",
                    original_tokens=0,
                    summary_tokens=0,
                    success=False,
                    error=msg,
                )

        summary = full_text.strip()
        # Strip the <analysis> block — keep only <summary> for the outer agent.
        summary = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL).strip()
        original_est = sum(len(str(m)) // 4 for m in messages)

        return AutoCompactResult(
            summary=summary,
            original_tokens=original_est,
            summary_tokens=len(summary) // 4,
            success=bool(summary),
        )

    except Exception as e:
        logger.warning("L4 autocompact failed: %s", e)
        return AutoCompactResult(
            summary="",
            original_tokens=0,
            summary_tokens=0,
            success=False,
            error=str(e),
        )


def _build_summary_prompt(messages: list[dict]) -> str:
    """Build a compact prompt for the summarizer sub-agent."""
    parts: list[str] = ["Summarize this coding session:\n\n"]

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, list):
            texts = [
                b.get("text", "")[:300]
                for b in content
                if b.get("type") == "text" and b.get("text", "").strip()
            ]
            tools = [
                b.get("name", "")
                for b in content
                if b.get("type") == "tool_use"
            ]
            for t in texts:
                parts.append(f"[{role}]: {t}\n")
            if tools:
                parts.append(f"[{role} used tools: {', '.join(tools)}]\n")
        elif isinstance(content, str):
            parts.append(f"[{role}]: {content[:300]}\n")

    return "".join(parts)


def inject_summary(
    messages: list[dict],
    summary: str,
    keep_recent: int = 2,
) -> list[dict]:
    """Replace conversation history with a summary + most recent exchanges.

    Keeps the first message (original task context) and the last N exchanges.
    Everything else is replaced by the summary.
    """
    if not summary or len(messages) <= keep_recent * 2:
        return messages

    summary_msg = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"[Session summary — earlier conversation compressed]\n\n{summary}",
            }
        ],
    }

    return [messages[0], summary_msg] + messages[-(keep_recent * 2):]


# ── Circuit breaker gate ──────────────────────────────────────────────


def should_autocompact(
    current_tokens: int,
    snip_tokens_freed: int = 0,
    context_limit: int = 200_000,
    consecutive_failures: int = 0,
    l3_suppressed: bool = False,
) -> bool:
    """Decide whether L4 autocompact should run using strict token budgets.

    The trigger threshold is computed as:
        effective_window = context_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY
        trigger_threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS

    L1 snip savings are deducted from current_tokens before comparison,
    so that snip-reduced contexts don't unnecessarily trigger the nuclear option.

    Args:
        current_tokens: Estimated token count for the current messages.
        snip_tokens_freed: Tokens already freed by L1 snip (deducted from
                           current_tokens before threshold comparison).
        context_limit: Model context window size in tokens.
        consecutive_failures: Count of consecutive autocompact failures.
        l3_suppressed: True if L3 collapse already handled the overflow.

    Returns:
        True if autocompact should proceed.
    """
    # 1. Circuit breaker — permanent disable after too many failures.
    if consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        logger.warning(
            "Autocompact 熔断器已触发（连续失败 %d 次），放弃压缩。",
            consecutive_failures,
        )
        return False

    # 2. L3 won the race — skip L4.
    if l3_suppressed:
        return False

    # 3. Strict token budget.
    effective_window = context_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY
    trigger_threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS

    # Deduct savings from L1 snip.
    adjusted_tokens = current_tokens - snip_tokens_freed

    return adjusted_tokens >= trigger_threshold


# ── PTL truncation ────────────────────────────────────────────────────


def _is_ptl_error(exception: Exception) -> bool:
    """Check whether an exception is a Prompt-Too-Long error."""
    msg = str(exception).lower()
    return any(kw in msg for kw in _PTL_ERROR_KEYWORDS)


def truncate_head_for_ptl_retry(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop the oldest complete API round from the head of the message list.

    A "round" is defined as:
      1. A leading ``user`` message.
      2. All immediately following ``assistant`` messages (which may contain
         tool_use blocks).
      3. The next ``user`` message that carries tool_result blocks (the
         tool-output reply).

    In other words, we cut from the first ``user`` up to (but not including)
    the next ``user`` that is NOT a pure tool-result carrier — i.e. the next
    "real" user prompt.

    **Safety invariant**: ``messages[0]`` is **never** removed.  It carries
    the initial task definition (or a ``system`` prompt in multi-agent
    setups).  Losing it would cause the sub-agent to lose the summarization
    goal entirely.

    If the list has too few messages (≤ 3), it is returned unchanged.

    The input list is never mutated — a new list is returned.
    """
    if len(messages) <= 3:
        return list(messages)

    result = list(messages)  # shallow copy is enough (we only slice, not mutate)

    # Find the first user message (start of oldest round).
    first_user_idx: int | None = None
    for i, msg in enumerate(result):
        if msg.get("role") == "user":
            first_user_idx = i
            break

    if first_user_idx is None:
        return result

    # Find the next user message AFTER the assistant block — this marks
    # the start of the second round.
    cut_idx: int | None = None
    for i in range(first_user_idx + 1, len(result)):
        role = result[i].get("role", "")
        if role == "user":
            cut_idx = i
            break

    if cut_idx is None or cut_idx >= len(result):
        # Only one round — can't truncate further without emptying history.
        return result

    # Preserve messages[0] (initial task definition / system prompt) and
    # drop the round between it and the cut point.
    if cut_idx > 0:
        return [result[0]] + result[cut_idx:]
    return result[cut_idx:]


# ── Retry wrapper ─────────────────────────────────────────────────────

# Type for an async LLM client callable:
#   (messages, system_prompt) -> str
LLMClientFunc = Callable[[list[dict[str, Any]], str], Awaitable[str]]


async def execute_autocompact_with_retry(
    messages: list[dict[str, Any]],
    system_prompt: str,
    llm_client_func: LLMClientFunc,
) -> str:
    """Call the LLM summarizer with PTL-aware truncation retry.

    Args:
        messages: The message history to summarize.
        system_prompt: The summarization system prompt.
        llm_client_func: An async callable (messages, system_prompt) → summary.

    Returns:
        The summary string.

    Raises:
        Exception: Re-raised if all PTL retries are exhausted, or if a
                   non-PTL error occurs.
    """
    current_messages = list(messages)

    for attempt in range(MAX_PTL_RETRIES + 1):
        try:
            summary = await llm_client_func(current_messages, system_prompt)
            return summary

        except Exception as exc:
            if not _is_ptl_error(exc):
                # Non-PTL errors are not retried.
                raise

            if attempt >= MAX_PTL_RETRIES:
                logger.error(
                    "PTL retry exhausted (%d attempts), giving up.",
                    attempt + 1,
                )
                raise

            logger.debug(
                "PTL error on autocompact attempt %d/%d: %s",
                attempt + 1, MAX_PTL_RETRIES + 1, exc,
            )
            current_messages = truncate_head_for_ptl_retry(current_messages)

    # Unreachable — satisfies the type checker.
    raise RuntimeError("unreachable")


# ── Post-compact memory restoration ───────────────────────────────────


def run_post_compact_cleanup(
    state: Any,
    recent_read_files: list[dict[str, str]],
) -> None:
    """Restore short-term memory after a nuclear autocompact.

    After L4 compresses the entire history into a summary, the agent loses
    the exact content of recently read files.  This function appends a
    recovery message containing truncated versions of those files so the
    agent can continue working without re-reading everything.

    Args:
        state: AgentState whose messages list will be appended to.
        recent_read_files: List of dicts with ``path`` and ``content`` keys,
            in chronological order (oldest first).
    """
    if not recent_read_files:
        return

    # Avoid duplicate recovery blocks from repeated autocompact calls.
    if any("[System: Post-compact memory restoration]" in str(m) for m in state.messages[-2:]):
        return

    files_to_restore = recent_read_files[-POST_COMPACT_MAX_FILES_TO_RESTORE:]
    restore_text_parts = ["[System: Post-compact memory restoration]"]

    for f in files_to_restore:
        path = f.get("path", "unknown")
        content = f.get("content", "")
        truncated_content = content[:POST_COMPACT_MAX_TOKENS_PER_FILE * 4]
        restore_text_parts.append(f"--- File: {path} ---\n{truncated_content}")

    recovery_msg = {
        "role": "user",
        "content": [{"type": "text", "text": "\n\n".join(restore_text_parts)}],
    }

    state.messages.append(recovery_msg)
