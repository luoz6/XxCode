"""Max-output-token recovery for text-only truncated model responses."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..tools import ToolCall
from .continue_reasons import ContinueReason
from .events import StreamEvent
from .messages import append_continuation_prompt, commit_assistant_turn
from .state import AgentState

logger = logging.getLogger(__name__)

MAX_OUTPUT_RECOVERY_RETRIES = 3
ESCALATED_MAX_TOKENS = 64_000


@dataclass
class OutputRecoveryState:
    """Per-session state for max output token recovery."""

    retries: int = 0
    escalated: bool = False
    current_max_tokens: int = 0

    @classmethod
    def from_config(cls, api_max_tokens: int) -> "OutputRecoveryState":
        return cls(current_max_tokens=api_max_tokens)

    def reset_retries(self) -> None:
        self.retries = 0

    def reset_after_history_replace(self, api_max_tokens: int) -> None:
        self.retries = 0
        self.escalated = False
        self.current_max_tokens = api_max_tokens


def is_output_truncated_stop_reason(reason: str) -> bool:
    """Check whether a stop_reason indicates the output was truncated."""
    return reason in ("max_tokens", "length", "token_limit")


@dataclass
class OutputRecoveryResult:
    """Decision returned by output recovery."""

    action: str
    event: StreamEvent | None = None
    reason: ContinueReason | None = None

    @property
    def should_continue(self) -> bool:
        return self.action in ("retry", "continue")

    @property
    def should_return(self) -> bool:
        return self.action == "fail"


def handle_output_truncation(
    *,
    state: AgentState,
    recovery: OutputRecoveryState,
    tool_calls: list[ToolCall],
    thinking_content: list[dict[str, Any]],
    full_text: str,
    current_message_id: str | None,
    input_tokens: int,
    output_tokens: int,
) -> OutputRecoveryResult:
    """Handle text-only output truncation.

    Truncated responses that contain tool calls must proceed to normal
    tool execution so the assistant tool_use stays paired with user
    tool_result blocks. This helper only recovers text-only truncation.
    """
    if tool_calls:
        return OutputRecoveryResult(action="proceed")

    if not recovery.escalated:
        recovery.escalated = True
        recovery.current_max_tokens = ESCALATED_MAX_TOKENS
        state.last_continue_reason = ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE
        logger.info(
            "[ContinueSite: max_output_tokens_escalate] "
            "Output truncated -> escalated max_tokens to %d, retrying. "
            "(partial output discarded)",
            ESCALATED_MAX_TOKENS,
        )
        return OutputRecoveryResult(
            action="retry",
            reason=ContinueReason.MAX_OUTPUT_TOKENS_ESCALATE,
        )

    if recovery.retries < MAX_OUTPUT_RECOVERY_RETRIES:
        recovery.retries += 1
        state.last_continue_reason = ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY
        logger.info(
            "[ContinueSite: max_output_tokens_recovery] "
            "Saving partial output then injecting continuation "
            "(attempt %d/%d).",
            recovery.retries, MAX_OUTPUT_RECOVERY_RETRIES,
        )

        commit_text_only_truncation(
            state=state,
            thinking_content=thinking_content,
            full_text=full_text,
            current_message_id=current_message_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return OutputRecoveryResult(
            action="continue",
            reason=ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY,
        )

    logger.error(
        "[max_output_tokens] Recovery exhausted after %d continuation retries.",
        MAX_OUTPUT_RECOVERY_RETRIES,
    )
    return OutputRecoveryResult(
        action="fail",
        event=StreamEvent(
            type="error",
            content=(
                f"Output repeatedly truncated after {MAX_OUTPUT_RECOVERY_RETRIES} "
                f"continuation attempts. The response may be too long for the model's "
                f"output limit. Try breaking the task into smaller steps."
            ),
        ),
    )


def commit_text_only_truncation(
    *,
    state: AgentState,
    thinking_content: list[dict[str, Any]],
    full_text: str,
    current_message_id: str | None,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Persist a truncated text-only assistant turn before continuation."""
    commit_assistant_turn(
        state,
        thinking_content=thinking_content,
        full_text=full_text,
        tool_calls=[],
        message_id=current_message_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    append_continuation_prompt(state)
