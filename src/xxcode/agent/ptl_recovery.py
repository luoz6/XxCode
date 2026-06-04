"""Prompt-too-long recovery helpers for the core agent loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .continue_reasons import ContinueReason
from .events import StreamEvent
from .state import AgentState

logger = logging.getLogger(__name__)

_PTL_ERROR_KEYWORDS = (
    "too_long", "token limit", "context length", "prompt is too long",
    "maximum context", "exceeds the maximum", "max_tokens",
    "input length", "reduce the length",
)


def is_ptl_error_message(msg: str) -> bool:
    """Check whether an error message indicates a Prompt-Too-Long condition."""
    low = msg.lower()
    return any(kw in low for kw in _PTL_ERROR_KEYWORDS)


@dataclass
class PTLRecoveryManager:
    """Owns L3 collapse regions and L4 reactive compaction."""

    config: Config
    regions: list[Any]

    async def try_collapse_drain(self, state: AgentState) -> bool:
        """Commit or create L3 collapse regions to free prompt tokens."""
        from ..context.collapse import collapse_messages
        from ..context.tokens import token_count_with_estimation

        before_tokens = token_count_with_estimation(state.messages)
        collapsed = collapse_messages(state.messages, keep_recent=5)
        after_tokens = token_count_with_estimation(collapsed)

        if after_tokens >= before_tokens or collapsed == state.messages:
            collapsed = collapse_messages(state.messages, keep_recent=2)
            after_tokens = token_count_with_estimation(collapsed)

        from .loop import _repair_orphan_tools

        collapsed = _repair_orphan_tools(collapsed)
        after_tokens = token_count_with_estimation(collapsed)

        if after_tokens < before_tokens and collapsed != state.messages:
            state.messages = collapsed
            self.regions = []
            state.cache_breakpoints.clear()
            state.last_continue_reason = ContinueReason.COLLAPSE_DRAIN_RETRY
            logger.info(
                "[PTL] collapse_drain: shortened request, %d -> %d tokens",
                before_tokens, after_tokens,
            )
            return True

        return False

    async def reactive_compact(self, state: AgentState) -> bool:
        """Run full L4 autocompact as a last resort for PTL recovery."""
        from ..context import ContextPipeline
        from ..context.tokens import token_count_with_estimation

        logger.info("[PTL] reactive_compact: running full L4 autocompact")

        try:
            pipeline = ContextPipeline(self.config)
            current_tokens = token_count_with_estimation(state.messages)
            compressed, stats = await pipeline.compress(
                state.messages,
                current_tokens=current_tokens,
                system_prompt=state.system_prompt,
                state=state,
            )

            if stats.level_reached >= 3:
                state.messages = compressed
                self.regions = []
                state.cache_breakpoints.clear()
                state.last_continue_reason = ContinueReason.REACTIVE_COMPACT_RETRY
                logger.info(
                    "[PTL] reactive_compact: success, %d -> %d tokens (level %d)",
                    stats.tokens_before, stats.tokens_after, stats.level_reached,
                )
                return True

            logger.warning(
                "[PTL] reactive_compact: insufficient, only reached level %d",
                stats.level_reached,
            )
            return False

        except Exception as exc:
            logger.exception("[PTL] reactive_compact failed: %s", exc)
            return False

    async def recover(self, state: AgentState, error_msg: str) -> tuple[str, StreamEvent | None]:
        """Try PTL recovery and return a loop action.

        Returns:
            ("retry", None): retry the model request.
            ("fail", event): emit the event, then stop the loop.
        """
        drained = await self.try_collapse_drain(state)
        if drained:
            logger.info(
                "[ContinueSite: collapse_drain_retry] "
                "PTL error after collapse drain - retrying request. "
                "Error: %s", error_msg[:200],
            )
            return "retry", None

        compacted = await self.reactive_compact(state)
        if compacted:
            logger.info(
                "[ContinueSite: reactive_compact_retry] "
                "PTL error after full compaction - retrying request. "
                "Error: %s", error_msg[:200],
            )
            return "retry", None

        logger.error(
            "[PTL] Both collapse drain and reactive compact failed. "
            "Original error: %s", error_msg[:200],
        )
        state.recent_api_errors.append(f"Prompt too long: {error_msg[:200]}")
        if len(state.recent_api_errors) > 5:
            state.recent_api_errors = state.recent_api_errors[-5:]

        return "fail", StreamEvent(
            type="error",
            content=(
                "Prompt too long - unable to reduce context enough. "
                "Please start a new session or manually trim the conversation."
            ),
        )


