"""Shared helpers for legacy and full-screen terminal UIs."""

from __future__ import annotations

from typing import Any


YOLO_LABEL = "\u26a1 YOLO"
TOOLBAR_SEPARATOR = " \u2502 "


def normalize_permission_answer(answer: str) -> str:
    """Normalize free-form permission input to the canonical decision ids."""
    normalized = (answer or "").strip().lower()
    if not normalized:
        return "no"

    direct_map = {
        "y": "yes",
        "yes": "yes",
        "n": "no",
        "no": "no",
        "a": "always",
        "always": "always",
        "d": "deny_all",
        "deny": "deny_all",
        "deny_all": "deny_all",
        "never": "deny_all",
    }
    mapped = direct_map.get(normalized)
    if mapped is not None:
        return mapped

    first = normalized[0]
    if first == "y":
        return "yes"
    if first == "n":
        return "no"
    if first == "a":
        return "always"
    if first == "d":
        return "deny_all"
    return "no"


def calculate_session_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> float:
    """Return the estimated session cost for the given token totals."""
    return ((input_tokens / 1000) * input_price_per_1k) + (
        (output_tokens / 1000) * output_price_per_1k
    )


def format_cwd_for_display(cwd: str, max_width: int = 55) -> str:
    """Middle-truncate a long cwd, preserving head and tail."""
    if len(cwd) <= max_width:
        return cwd
    head = cwd[: max_width // 2]
    tail = cwd[-(max_width // 2 - 3):]
    return f"{head}...{tail}"


def build_session_toolbar(
    state: Any,
    *,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> str:
    """Build a compact bottom-toolbar summary from session state."""
    if state is None:
        return ""

    parts: list[str] = []
    turns = int(getattr(state, "turn_count", 0) or 0)
    if turns > 0:
        parts.append(f"T{turns}")

    input_tokens = int(getattr(state, "total_input_tokens", 0) or 0)
    output_tokens = int(getattr(state, "total_output_tokens", 0) or 0)
    total_tokens = input_tokens + output_tokens
    if total_tokens > 0:
        if total_tokens >= 1000:
            parts.append(f"{total_tokens // 1000}K tok")
        else:
            parts.append(f"{total_tokens} tok")

    total_cost = calculate_session_cost(
        input_tokens,
        output_tokens,
        input_price_per_1k=input_price_per_1k,
        output_price_per_1k=output_price_per_1k,
    )
    if total_cost > 0.0001:
        parts.append(f"${total_cost:.4f}")

    permission_state = getattr(state, "permission_state", None)
    if permission_state is not None and getattr(permission_state, "yolo_mode", False):
        parts.append(YOLO_LABEL)

    return TOOLBAR_SEPARATOR.join(parts)
