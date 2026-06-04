"""L3: Context Collapse — fold verbose exchanges into projection summaries.

When L1 snip + L2 micro are insufficient, collapse older conversation turns
into lightweight ProjectedExchange records that preserve intent and structure
while discarding verbatim detail.

Design principle: "读写分离" (read-write separation).  The original message
history is never mutated.  Instead, a sidecar list of CollapsedRegion records
is maintained.  At read time, `project_collapsed_view` dynamically computes a
folded view for the API call.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# Number of recent exchanges to keep in full
_DEFAULT_KEEP_RECENT = 5

# Default token threshold that triggers L3 collapse.
_DEFAULT_COLLAPSE_THRESHOLD_TOKENS = 90_000

# Minimum number of consecutive uncollapsed messages to form a new region.
_MIN_REGION_SIZE = 3


# ── Sidecar data structures ──────────────────────────────────────────


@dataclass
class CollapsedRegion:
    """A folded interval in the original message history.

    Attributes:
        start_idx: First message index (inclusive) of the collapsed span.
        end_idx: Last message index (exclusive, Python slice convention).
        summary: Placeholder text that replaces this span in the projected view.
    """

    start_idx: int
    end_idx: int
    summary: str


@dataclass
class ProjectedExchange:
    """A condensed view of one conversation turn — preserves structure, not verbosity."""

    role: str = ""                          # "user" | "assistant"
    summary: str = ""                       # 1-2 sentence summary
    tool_calls: list[str] = field(default_factory=list)  # Tool names called
    key_outputs: list[str] = field(default_factory=list)  # Key output snippets


def collapse_messages(
    messages: list[dict],
    keep_recent: int = _DEFAULT_KEEP_RECENT,
) -> list[dict]:
    """Fold older conversation turns into projection summaries.

    Strategy:
      1. Find exchange boundaries (user → assistant → user → ...)
      2. Keep the most recent `keep_recent` exchanges in full
      3. Collapse older exchanges into ProjectedExchange summaries
      4. Insert a single "context summary" message with all projections

    Args:
        messages: Raw message list.
        keep_recent: Number of recent exchanges to preserve verbatim.

    Returns:
        New message list with older content collapsed.
    """
    if not messages:
        return messages

    exchanges = _partition_exchanges(messages)

    if len(exchanges) <= keep_recent:
        return list(messages)  # Nothing to collapse

    # Split: older exchanges → collapsed, recent → preserved
    older = exchanges[:-keep_recent]
    recent = exchanges[-keep_recent:]

    projections: list[ProjectedExchange] = []
    for exchange in older:
        proj = _project_exchange(exchange)
        if proj:
            projections.append(proj)

    if not projections:
        return list(messages)

    # Build the collapsed message
    collapsed_text = _build_projection_text(projections)

    # Reconstruct message list: collapsed summary + recent messages
    result: list[dict] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": collapsed_text}],
        }
    ]

    for exchange in recent:
        result.extend(exchange)

    return result


def _partition_exchanges(messages: list[dict]) -> list[list[dict]]:
    """Partition messages into exchanges.

    An exchange is: [user_msg, assistant_msg?] or [assistant_msg, user_msg?].
    Uses role alternation as the primary boundary heuristic.
    """
    if not messages:
        return []

    exchanges: list[list[dict]] = []
    current: list[dict] = [messages[0]]
    last_role = messages[0].get("role", "")

    for msg in messages[1:]:
        role = msg.get("role", "")
        # New exchange on user→assistant or assistant→user transition
        # But NOT on multiple assistant messages in a row (same turn, multi-block)
        if role != last_role and current:
            exchanges.append(current)
            current = [msg]
        else:
            current.append(msg)
        last_role = role

    if current:
        exchanges.append(current)

    return exchanges


def _project_exchange(exchange: list[dict]) -> ProjectedExchange | None:
    """Condense a single exchange into a ProjectedExchange."""
    if not exchange:
        return None

    proj = ProjectedExchange()

    for msg in exchange:
        role = msg.get("role", "")
        if not proj.role:
            proj.role = role

        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            block_type = block.get("type", "")

            if block_type == "text":
                text = block.get("text", "")
                if text and len(text) > 10:
                    # Use first sentence as summary
                    first_sent = text.split(".")[0].strip()[:200]
                    if not proj.summary:
                        proj.summary = first_sent

            elif block_type == "tool_use":
                tool_name = block.get("name", "")
                if tool_name:
                    proj.tool_calls.append(tool_name)

            elif block_type == "tool_result":
                result = block.get("content", "")
                if result:
                    # Keep first line of tool output
                    first_line = result.strip().split("\n")[0][:150]
                    if first_line:
                        proj.key_outputs.append(first_line)

    # Don't return empty projections
    if not proj.summary and not proj.tool_calls:
        return None

    return proj


def _build_projection_text(projections: list[ProjectedExchange]) -> str:
    """Build a text block summarizing collapsed exchanges."""
    lines = ["[Earlier conversation — summarized]\n"]

    for i, proj in enumerate(projections, 1):
        parts = [f"Turn {i}"]
        if proj.summary:
            parts.append(f"  Summary: {proj.summary}")
        if proj.tool_calls:
            parts.append(f"  Tools called: {', '.join(proj.tool_calls)}")
        if proj.key_outputs:
            outputs = "; ".join(proj.key_outputs[:3])
            parts.append(f"  Key outputs: {outputs}")
        lines.append(" ".join(parts))

    return "\n".join(lines)


# ── Sidecar-based collapse (L3 proper) ───────────────────────────────
#
# These functions implement the "read-write separation" design:
#   1. apply_collapse_if_needed  — decides *when* to collapse and records
#      CollapsedRegion entries in a sidecar list.
#   2. project_collapsed_view    — at API-call time, produces a projected
#      view of the message history with collapsed regions replaced by
#      summary placeholders.
#
# The original messages list is NEVER mutated.


def _find_uncollapsed_span(
    messages: list[dict[str, Any]],
    existing_regions: list[CollapsedRegion],
    min_size: int = _MIN_REGION_SIZE,
) -> tuple[int, int] | None:
    """Find the oldest contiguous span of uncollapsed messages.

    Returns (start_idx, end_idx) for the first eligible span, or None
    if no span of at least *min_size* messages exists.
    """
    collapsed: set[int] = set()
    for region in existing_regions:
        for i in range(region.start_idx, region.end_idx):
            collapsed.add(i)

    # Walk from the beginning to find the first uncollapsed run.
    run_start = -1
    for i in range(len(messages)):
        if i not in collapsed:
            if run_start == -1:
                run_start = i
        else:
            if run_start != -1 and (i - run_start) >= min_size:
                return (run_start, i)
            run_start = -1

    # Check trailing run
    if run_start != -1 and (len(messages) - run_start) >= min_size:
        return (run_start, len(messages))

    return None


def project_collapsed_view(
    messages: list[dict[str, Any]],
    regions: list[CollapsedRegion],
) -> list[dict[str, Any]]:
    """Build a projected view: regions replaced by summary messages.

    **IMPORTANT — Read-time projection convention**:
    This function MUST be called at the *last possible moment* before the
    API call.  Any intermediate pipeline stages (loggers, diagnostic tools,
    full-text search helpers) must operate on the original ``messages``
    list — they see the complete history.  Only the API-bound view is
    projected.  Calling this too early makes downstream components believe
    history was deleted.

    Messages inside any CollapsedRegion are omitted.  At the position
    where the region *would* have appeared, a single summary message is
    inserted:

        {"role": "user", "content": [{"type": "text", "text": region.summary}]}

    Messages outside all regions pass through unchanged.

    Args:
        messages: The original (immutable) message history.
        regions:  Sidecar list of CollapsedRegion records.

    Returns:
        A brand-new list[dict] — never mutates the input.
    """
    if not regions:
        return copy.deepcopy(messages)

    # Sort regions by start_idx for deterministic insertion order.
    sorted_regions = sorted(regions, key=lambda r: r.start_idx)

    # Build a quick lookup: message index → summary text to insert
    # (only the first message of each region triggers the insertion).
    insert_map: dict[int, str] = {}
    skip_indices: set[int] = set()
    for region in sorted_regions:
        insert_map[region.start_idx] = region.summary
        for i in range(region.start_idx, region.end_idx):
            skip_indices.add(i)

    result: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i in skip_indices:
            # If this is the first index of the region, emit the summary.
            if i in insert_map:
                result.append({
                    "role": "user",
                    "content": [{"type": "text", "text": insert_map[i]}],
                })
            continue
        # Deep-copy the message so the caller can't accidentally mutate originals.
        result.append(copy.deepcopy(msg))

    return result


def apply_collapse_if_needed(
    messages: list[dict[str, Any]],
    current_tokens: int,
    collapse_threshold_tokens: int = _DEFAULT_COLLAPSE_THRESHOLD_TOKENS,
    existing_regions: list[CollapsedRegion] | None = None,
) -> tuple[bool, list[CollapsedRegion]]:
    """Decide whether to collapse another span of the conversation.

    Args:
        messages:              The full message history.
        current_tokens:        Estimated token count (from pipeline).
        collapse_threshold_tokens:  Token count that triggers L3.
        existing_regions:      Already-collapsed regions (None → first call).

    Returns:
        (suppress_autocompact, updated_regions)

        *suppress_autocompact* is True when L3 successfully collapsed a
        span — the caller should skip L4 autocompact to avoid the more
        destructive nuclear option.

    If *current_tokens* is below the threshold, returns (False, …)
    without creating a new region.
    """
    regions: list[CollapsedRegion] = list(existing_regions or [])

    # Below threshold — nothing to do.
    if current_tokens < collapse_threshold_tokens:
        return (False, regions)

    # Find the oldest uncollapsed span.
    span = _find_uncollapsed_span(messages, regions)
    if span is None:
        # Everything already collapsed — fall through to L4.
        return (False, regions)

    start, end = span
    msg_count = end - start

    # Generate a static summary for the collapsed span.
    summary = (
        f"[System: {msg_count} turns of debugging logs collapsed "
        f"for context efficiency. Original messages {start}–{end - 1} "
        f"contained verbose tool output that has been folded.]"
    )

    regions.append(CollapsedRegion(start_idx=start, end_idx=end, summary=summary))
    return (True, regions)


# ── Pipeline integration guide ────────────────────────────────────────
#
# Example usage inside ContextPipeline.compress():
#
#   # ── L3: Context Collapse ──────────────────────────────────────
#   current_tokens = estimate_tokens(current, system_prompt)
#   suppress_auto, regions = apply_collapse_if_needed(
#       messages=current,
#       current_tokens=current_tokens,
#       collapse_threshold_tokens=90_000,
#       existing_regions=getattr(self, '_l3_regions', None),
#   )
#   self._l3_regions = regions
#
#   if suppress_auto:
#       # Project the collapsed view for the API call.
#       current = project_collapsed_view(current, regions)
#       stats.level_reached = 3
#       stats.tokens_after = estimate_tokens(current, system_prompt)
#       return current, stats
#
#   # ── L4: Autocompact (only reached if L3 didn't fire) ──────────
#   ...
