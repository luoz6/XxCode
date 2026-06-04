"""Structured continue reasons for agent-loop retries and continuations."""

from __future__ import annotations

from enum import StrEnum


class ContinueReason(StrEnum):
    NEXT_TURN = "next_turn"
    COLLAPSE_DRAIN_RETRY = "collapse_drain_retry"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
