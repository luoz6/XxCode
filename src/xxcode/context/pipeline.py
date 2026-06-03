"""Core scheduler — chains the 4-level compression pipeline.

Progressive escalation:
  L1 snip → L2 micro → L3 collapse → L4 auto
Each level is more aggressive and expensive than the previous.
After each level, token count is re-estimated. If within limit, stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Config
from .auto import should_autocompact
from .collapse import collapse_messages
from .micro import microcompact_messages
from .snip import snip_messages
from .tokens import rough_estimate, token_count_with_estimation

logger = logging.getLogger(__name__)


@dataclass
class CompressionStats:
    """Statistics from a compression pass."""

    level_reached: int = 0        # Last level triggered (0 = no compression needed)
    tokens_before: int = 0
    tokens_after: int = 0
    snip_tokens_freed: int = 0
    micro_tokens_freed: int = 0
    collapse_tokens_freed: int = 0
    auto_tokens_freed: int = 0
    snip_removed: int = 0         # L1: approximate characters trimmed
    micro_truncated: int = 0      # L2: number of results truncated
    micro_cleared: int = 0
    collapse_count: int = 0       # L3: exchanges collapsed
    auto_triggered: bool = False  # L4: sub-agent summary triggered


class ContextPipeline:
    """Four-level progressive compression pipeline.

    Usage:
        pipeline = ContextPipeline(config)
        compressed, stats = await pipeline.compress(messages, system_prompt)
    """

    def __init__(self, config: Config | None = None):
        from ..config import get_config

        self.config = config or get_config()
        self._autocompacter = None  # Lazy init for L4
        self._consecutive_autocompact_failures = 0

    # ── Public API ────────────────────────────────────────────────

    async def compress(
        self,
        messages: list[dict],
        current_tokens: int | None = None,
        system_prompt: str = "",
        context_limit: int = 200_000,
        threshold: float | None = None,
        state: Any = None,
    ) -> tuple[list[dict], CompressionStats]:
        """Run progressive compression on the message list.

        Args:
            messages: Messages to compress.
            current_tokens: Pre-computed token count (from
                            ``token_count_with_estimation``).  If None,
                            computed on entry.
            system_prompt: Current system prompt (for context).
            context_limit: Model context window size in tokens.
            threshold: Fraction of context_limit that triggers compression.
            state: Optional AgentState for budget carryover on L4.

        Returns:
            (compressed_messages, stats)
        """
        if threshold is None:
            threshold = self.config.context_compress_threshold

        soft_limit = int(context_limit * threshold)
        stats = CompressionStats()

        current = list(messages)
        stats.tokens_before = (
            current_tokens
            if current_tokens is not None
            else token_count_with_estimation(current)
        )

        if stats.tokens_before <= soft_limit:
            stats.tokens_after = stats.tokens_before
            return current, stats

        # ── L1: Snip ──────────────────────────────────────────────
        logger.debug("L1 snip: %d tokens → %d limit", stats.tokens_before, soft_limit)
        before_chars = _total_result_chars(current)
        current = snip_messages(current)
        after_chars = _total_result_chars(current)
        stats.snip_removed = max(0, before_chars - after_chars)
        stats.level_reached = 1

        post_l1_tokens = token_count_with_estimation(current)
        stats.snip_tokens_freed = stats.tokens_before - post_l1_tokens
        stats.tokens_after = post_l1_tokens
        if stats.tokens_after <= soft_limit:
            return current, stats

        # ── L2: Microcompact ──────────────────────────────────────
        logger.debug("L2 micro: %d tokens still over limit", stats.tokens_after)
        before_results = _count_tool_results(current)
        current, _edits = microcompact_messages(current, is_cache_cold=True, keep_recent=1)
        after_results = sum(
            1 for m in current
            for b in (m.get("content", []) if isinstance(m.get("content"), list) else [])
            if b.get("type") == "tool_result"
        )
        stats.micro_truncated = before_results
        stats.level_reached = 2

        stats.tokens_after = token_count_with_estimation(current)
        if stats.tokens_after <= soft_limit:
            return current, stats

        # ── L3: Collapse ──────────────────────────────────────────
        logger.debug("L3 collapse: %d tokens still over limit", stats.tokens_after)
        before_msgs = len(current)
        current = collapse_messages(current, keep_recent=5)
        stats.collapse_count = max(0, before_msgs - len(current))
        stats.level_reached = 3

        stats.tokens_after = token_count_with_estimation(current)
        if stats.tokens_after <= soft_limit:
            return current, stats

        # ── L4: Autocompact ───────────────────────────────────────

        # Check whether the nuclear option should actually fire.
        # Pass snip_tokens_freed so L1 savings are properly credited —
        # without this, the trigger underestimates how much headroom exists.
        failure_count = (
            int(state.consecutive_autocompact_failures)
            if state is not None and hasattr(state, "consecutive_autocompact_failures")
            else self._consecutive_autocompact_failures
        )
        snip_tokens_freed = stats.snip_removed // 4
        if not should_autocompact(
            current_tokens=stats.tokens_after,
            snip_tokens_freed=snip_tokens_freed,
            context_limit=context_limit,
            consecutive_failures=failure_count,
        ):
            logger.debug(
                "L4 suppressed: tokens_after=%d, snip_freed=%d, "
                "consecutive_failures=%d",
                stats.tokens_after, snip_tokens_freed, failure_count,
            )
            stats.tokens_after = token_count_with_estimation(current)
            return current, stats

        logger.debug("L4 auto: nuclear option triggered")

        # Budget carryover: deduct pre-compact waterline BEFORE replacing history.
        #
        # WARNING: 必须扣除压缩前的水位，否则系统会因为历史记录变短
        #          而误以为预算又恢复了。
        if state is not None and getattr(state, "task_budget_remaining", None) is not None:
            final_tokens_before_nuke = token_count_with_estimation(current)
            state.task_budget_remaining -= final_tokens_before_nuke
            logger.debug(
                "L4 budget carryover: deducted %d tokens, %d remaining",
                final_tokens_before_nuke, state.task_budget_remaining,
            )

        stats.auto_triggered = True
        stats.level_reached = 4

        try:
            summary = await self._autocompact(current, system_prompt)
            current = _inject_summary(current, summary, keep_recent=2)
            # Success — reset the failure counter.
            self._consecutive_autocompact_failures = 0
            if state is not None and hasattr(state, "consecutive_autocompact_failures"):
                state.consecutive_autocompact_failures = 0
        except Exception as e:
            logger.warning("L4 autocompact failed: %s", e)
            if state is not None and hasattr(state, "consecutive_autocompact_failures"):
                state.consecutive_autocompact_failures += 1
                self._consecutive_autocompact_failures = state.consecutive_autocompact_failures
            else:
                self._consecutive_autocompact_failures += 1

        stats.tokens_after = token_count_with_estimation(current)
        return current, stats

    def estimate_tokens(self, messages: list[dict], system_prompt: str = "") -> int:
        """Estimate token count for messages using the anchor algorithm."""
        return token_count_with_estimation(messages)

    # ── L4 helpers ────────────────────────────────────────────────

    async def _autocompact(self, messages: list[dict], system_prompt: str) -> str:
        """Generate a full-conversation summary via the API."""
        from ..api.client import create_llm_client
        from ..api.retry import RetryConfig

        client = create_llm_client(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.api_model,
            max_tokens=500,
            retry_config=RetryConfig(),
        )

        # Build a minimal prompt asking for a structured summary
        summary_prompt = _build_autocompact_prompt(messages)

        compact_system = (
            "You are a conversation summarizer. Create a concise structured summary "
            "of the conversation below. Include: key decisions made, files modified, "
            "errors encountered, and the current task state. Keep it under 200 words."
        )

        try:
            full_text = ""
            async for event in client.stream_chat(
                system_prompt=compact_system,
                messages=[{"role": "user", "content": [{"type": "text", "text": summary_prompt}]}],
                tools=[],
            ):
                if event["type"] == "text_delta":
                    full_text += event["text"]
                elif event["type"] == "error":
                    logger.warning("L4 autocompact API error: %s", event.get("message", ""))
                    break
            return full_text.strip()
        except Exception:
            return ""


def _build_autocompact_prompt(messages: list[dict]) -> str:
    """Build a prompt for the autocompact sub-agent."""
    parts: list[str] = ["Summarize this conversation:\n\n"]
    for msg in messages:
        role = msg.get("role", "system")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Extract text blocks only for summary prompt
            texts = [
                b.get("text", "") for b in content
                if b.get("type") in ("text",)
                and len(b.get("text", "")) > 0
            ]
            for t in texts:
                parts.append(f"[{role}]: {t[:500]}\n")
            # Note tool calls
            tools = [
                b.get("name", "") for b in content
                if b.get("type") == "tool_use"
            ]
            if tools:
                parts.append(f"[{role} called tools: {', '.join(tools)}]\n")
        elif isinstance(content, str):
            parts.append(f"[{role}]: {content[:500]}\n")
    return "".join(parts)


def _inject_summary(
    messages: list[dict], summary: str, keep_recent: int = 2
) -> list[dict]:
    """Replace old messages with a summary, keeping the most recent exchanges."""
    if not summary:
        return messages

    if len(messages) <= keep_recent * 2:
        return messages

    # Insert summary as a system-like user message
    summary_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"[Conversation summary]\n{summary}"}
        ],
    }

    # Keep the first message (original user prompt) + recent exchanges
    return [messages[0], summary_msg] + messages[-(keep_recent * 2):]


# ── Helpers ───────────────────────────────────────────────────────────


def _total_result_chars(messages: list[dict]) -> int:
    """Sum character count of all tool_result blocks."""
    total = 0
    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    total += len(str(block.get("content", "")))
    return total


def _count_tool_results(messages: list[dict]) -> int:
    """Count tool_result blocks in the message list."""
    count = 0
    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    count += 1
    return count
