"""High-precision token estimation using API usage anchors.

Design:
  - No dependency on tiktoken or heavy tokenizers.
  - The API's ``usage`` block (input_tokens + output_tokens) serves as an
    *anchor* — a known-exact measurement at a specific point in history.
  - Messages after the anchor are estimated with the rough 1-token ≈ 4‑chars
    heuristic.
  - The result is:  exact_anchor + rough_delta.

This gives near-exact counts while remaining fast and offline.
"""

from __future__ import annotations

from typing import Any


def rough_estimate(source: list[dict[str, Any]] | str) -> int:
    """Fast, offline token estimate: 1 token ≈ 4 characters.

    Accepts either a message list or a single string.  When given a string,
    simply returns ``len(source) // 4`` — useful for delta calculations
    (e.g. comparing two tool_result strings).
    """
    if isinstance(source, str):
        return len(source) // 4

    total = 0
    for msg in source:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block)) // 4
        elif isinstance(content, str):
            total += len(content) // 4
    return total


def token_count_with_estimation(
    messages: list[dict[str, Any]],
) -> int:
    """Estimate total tokens using the last API usage block as an anchor.

    Algorithm (reverse scan):
      1. Walk *backwards* from the last message looking for an assistant
         message that carries a ``usage`` dict.
      2. Once found, record its ``id``.  Continue walking backwards — if the
         preceding message has the same ``id`` it is a split response from
         the same API call; extend the anchor to cover it too.
      3. The anchor's exact token count is ``usage.input_tokens + usage.output_tokens``.
      4. All messages *after* the (possibly extended) anchor are estimated
         with :func:`rough_estimate`.
      5. Return:  exact_anchor + rough_estimate(post_anchor_messages).

    If no message with ``usage`` is found, the entire list is estimated via
    :func:`rough_estimate`.

    Args:
        messages: The full message history (must include API response metadata
                  such as ``usage`` and ``id`` on assistant messages).

    Returns:
        Estimated total token count (int).
    """
    if not messages:
        return 0

    n = len(messages)

    # ── Step 1: Find the last anchor (assistant + usage) ──────────
    anchor_idx: int | None = None
    for i in range(n - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if isinstance(usage, dict) and "input_tokens" in usage:
            anchor_idx = i
            break

    if anchor_idx is None:
        # No anchor — fall back to full rough estimate.
        return rough_estimate(messages)

    # ── Step 2: Extend anchor for split responses (same message id) ─
    anchor_msg = messages[anchor_idx]
    anchor_id = anchor_msg.get("id")

    if anchor_id is not None:
        # Walk backwards while the preceding message shares the same id.
        while anchor_idx > 0:
            prev = messages[anchor_idx - 1]
            if prev.get("id") == anchor_id:
                anchor_idx -= 1
            else:
                break

    # ── Step 3: Exact anchor count ─────────────────────────────────
    usage = messages[anchor_idx].get("usage", {})
    # Use the *last* message in the split group for usage (it has the totals).
    usage_last = messages[anchor_idx]
    # Walk forward within the split group to find the message that actually has usage.
    for j in range(anchor_idx, n):
        u = messages[j].get("usage")
        if isinstance(u, dict) and "input_tokens" in u:
            usage = u
            break

    exact_base = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    # Find the end of the anchor group (last message with the same id).
    anchor_end_idx = anchor_idx
    if anchor_id is not None:
        for j in range(anchor_idx + 1, n):
            if messages[j].get("id") == anchor_id:
                anchor_end_idx = j
            else:
                break

    # ── Step 4: Rough estimate of post-anchor messages ─────────────
    post_anchor = messages[anchor_end_idx + 1:]
    delta = rough_estimate(post_anchor)

    return exact_base + delta


# ── Integration example: budget carryover across autocompact ───────────
#
# The following pseudocode shows how to correctly deduct the token budget
# before an L4/L5 autocompact replaces the message history.
#
#   # ── Inside ContextPipeline.compress() or equivalent ────────────
#
#   if should_autocompact(...):
#       # WARNING: 必须扣除压缩前的水位，否则系统会因为历史记录变短
#       #          而误以为预算又恢复了。
#       #
#       # 1. Measure the pre-compact waterline.
#       pre_tokens = token_count_with_estimation(state.messages)
#
#       # 2. Deduct from the remaining budget (if a budget is set).
#       if state.task_budget_remaining is not None:
#           state.task_budget_remaining -= pre_tokens
#           if state.task_budget_remaining < 0:
#               logger.warning(
#                   "Task budget exceeded by %d tokens", -state.task_budget_remaining
#               )
#
#       # 3. Run the summarizer.
#       summary = await execute_autocompact_with_retry(...)
#
#       # 4. Replace history with summary (old messages are discarded).
#       state.messages = inject_summary(state.messages, summary)
#
#       # NOTE: After step 4, DO NOT recalculate token_count_with_estimation
#       # and add it back to the budget.  The pre_tokens deduction already
#       # paid for the discarded history.  The new summary messages will be
#       # counted when the next anchor point is established.
#
#   # ── On initial budget setup ────────────────────────────────────
#
#   if args.budget is not None:
#       state.total_task_budget = args.budget
#       state.task_budget_remaining = args.budget
#
#   # ── Before each API call ───────────────────────────────────────
#
#   if state.task_budget_remaining is not None:
#       estimated = token_count_with_estimation(state.messages)
#       if estimated >= state.task_budget_remaining:
#           raise BudgetExceededError(
#               f"Estimated {estimated} tokens exceeds remaining budget "
#               f"of {state.task_budget_remaining}"
#           )