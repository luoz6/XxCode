"""Shared helpers for legacy and full-screen terminal UIs."""

from __future__ import annotations

from typing import Any


RICH_UNICODE = "rich_unicode"
ASCII_SAFE = "ascii_safe"

PHASE1_PERMISSION_ACTION_LABELS = ("允许一次", "本会话总是允许", "拒绝")
DISPLAY_RISK_LABELS = {
    "low": "低风险",
    "medium": "需确认",
    "high": "高风险",
}

_RICH_SYMBOLS = {
    "prompt.normal": "❯",
    "prompt.yolo": "⚡",
    "toolbar.separator": " │ ",
    "marker.success": "✓",
    "marker.error": "✗",
    "marker.pointer": "❯",
    "marker.permission": "⏺",
    "tool.read_file": "📖",
    "tool.write_file": "✍️",
    "tool.edit_file": "📝",
    "tool.grep_search": "🔍",
    "tool.glob_match": "🔎",
    "tool.run_shell": "💻",
    "tool.default": "🔧",
}

_ASCII_SYMBOLS = {
    "prompt.normal": ">",
    "prompt.yolo": "!",
    "toolbar.separator": " | ",
    "marker.success": "OK",
    "marker.error": "X",
    "marker.pointer": ">",
    "marker.permission": "*",
    "tool.read_file": "[R]",
    "tool.write_file": "[W]",
    "tool.edit_file": "[E]",
    "tool.grep_search": "[G]",
    "tool.glob_match": "[O]",
    "tool.run_shell": "[S]",
    "tool.default": "[T]",
}


YOLO_LABEL = "\u26a1 YOLO"
TOOLBAR_SEPARATOR = _RICH_SYMBOLS["toolbar.separator"]


def detect_display_mode(encoding: str | None) -> str:
    normalized = (encoding or "").strip().lower()
    if normalized in ("utf-8", "utf8", "utf_8", "cp65001"):
        return RICH_UNICODE
    return ASCII_SAFE


def get_display_symbols(mode: str) -> dict[str, str]:
    if mode == RICH_UNICODE:
        return dict(_RICH_SYMBOLS)
    return dict(_ASCII_SYMBOLS)


def translate_backend_risk_level(level: str) -> str:
    normalized = (level or "").strip().lower()
    if normalized == "high":
        return "high"
    if normalized == "normal":
        return "medium"
    if normalized in ("low", "medium", "high"):
        return normalized
    return "medium"


def normalize_permission_answer(answer: str) -> str:
    """Normalize free-form permission input to the canonical decision ids."""
    normalized = (answer or "").strip().lower()
    if not normalized:
        return "deny"

    direct_map = {
        "y": "once",
        "yes": "once",
        "n": "deny",
        "no": "deny",
        "a": "always",
        "always": "always",
        "d": "deny",
        "deny": "deny",
        "never": "deny",
    }
    mapped = direct_map.get(normalized)
    if mapped is not None:
        return mapped

    first = normalized[0]
    if first == "y":
        return "once"
    if first == "a":
        return "always"
    return "deny"


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
    separator: str = TOOLBAR_SEPARATOR,
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

    return separator.join(parts)
