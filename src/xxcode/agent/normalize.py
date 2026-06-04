"""Message normalization pipeline — 7 steps to produce API-ready messages.

All step functions are pure: they return new lists, never mutate inputs.
The orchestrator `normalize_messages()` applies the full pipeline in fixed order.
"""

from __future__ import annotations

from collections.abc import Iterable

# ── Error-to-block-type patterns ───────────────────────────────────

_ERROR_PATTERNS: dict[str, list[str]] = {
    "PDF too large": ["document"],
    "image too large": ["image"],
    "file too large": ["file"],
    "unsupported_media": ["image", "document"],
}

# Model families that support extended thinking (prefix match)
_THINKING_MODELS: tuple[str, ...] = (
    "deepseek",       # deepseek-v4-pro, deepseek-v4-flash (reasoning-capable)
    "claude-3-7",     # claude-3-7-sonnet
    "claude-4",       # claude-4-opus, claude-4-sonnet
    "claude-sonnet-4",  # claude-sonnet-4-6
    "claude-opus-4",    # claude-opus-4-7
)

# Content block types that are attachments
_ATTACHMENT_TYPES: tuple[str, ...] = ("image", "document", "file")

# Internal content block types to always strip
_INTERNAL_BLOCK_TYPES: tuple[str, ...] = ("tool_reference", "advisor_block")

# Content block types related to thinking
_THINKING_BLOCK_TYPES: tuple[str, ...] = ("thinking", "redacted_thinking", "signature")


# ── Step 1: Reorder attachments ────────────────────────────────────

def reorder_attachments(messages: list[dict]) -> list[dict]:
    """Bubble attachment blocks (image/document/file) to the front of each message.

    Within each message's content list, move attachment-type blocks before
    non-attachment blocks while preserving relative order within each group.
    """
    result: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        attachments = [b for b in content if b.get("type") in _ATTACHMENT_TYPES]
        others = [b for b in content if b.get("type") not in _ATTACHMENT_TYPES]
        new_content = attachments + others

        if new_content == content:
            result.append(msg)
        else:
            result.append({**msg, "content": new_content})
    return result


# ── Step 2: Filter virtual messages ─────────────────────────────────

def filter_virtual_messages(messages: list[dict]) -> list[dict]:
    """Remove messages marked as UI-only virtual messages."""
    return [m for m in messages if not m.get("isVirtual")]


# ── Step 3: Build error-to-block-type map ──────────────────────────

def build_error_block_map(recent_errors: list[str]) -> dict[str, list[str]]:
    """Scan recent API errors for known media-too-large patterns.

    Returns a dict mapping matched error keywords to the block types
    that must be stripped from subsequent requests.
    """
    mapping: dict[str, list[str]] = {}
    for error in recent_errors:
        for keyword, block_types in _ERROR_PATTERNS.items():
            if keyword in error:
                mapping[keyword] = block_types
    return mapping


# ── Step 4: Strip internal elements ────────────────────────────────

def strip_internal_elements(
    messages: list[dict],
    error_block_map: dict[str, list[str]],
) -> list[dict]:
    """Remove internal-only and error-triggered content blocks from messages.

    Strips:
      - tool_reference and advisor_block types (internal tracking)
      - Media blocks whose type matches entries in error_block_map
      - Entire messages that become empty after stripping
    """
    # Collect all block types to strip from error patterns
    strip_types: set[str] = set()
    for block_types in error_block_map.values():
        strip_types.update(block_types)

    result: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_content = [
            b for b in content
            if b.get("type") not in _INTERNAL_BLOCK_TYPES
            and b.get("type") not in strip_types
        ]

        if new_content:
            result.append({**msg, "content": new_content})
        # Skip messages that become empty after stripping

    return result


# ── Step 5: Handle thinking/signature blocks ────────────────────────

def _model_supports_thinking(model_family: str) -> bool:
    """Check if the model family supports extended thinking."""
    model_lower = model_family.lower()
    return any(model_lower.startswith(prefix) for prefix in _THINKING_MODELS)


def handle_thinking_blocks(
    messages: list[dict],
    model_family: str,
) -> list[dict]:
    """Strip thinking/signature blocks for models that don't support them.

    Also strips thinking blocks from user-role messages regardless of model
    (the API forbids them in user messages).
    """
    supports = _model_supports_thinking(model_family)
    result: list[dict] = []

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        role = msg.get("role", "")

        if role == "user" or not supports:
            # Strip ALL thinking-related blocks from user messages,
            # and from all messages for unsupported models
            new_content = _strip_thinking_blocks(content)
        else:
            # Supported model + assistant role: pass through
            new_content = list(content)

        if new_content:
            result.append({**msg, "content": new_content})

    return result


def _strip_thinking_blocks(content: list[dict]) -> list[dict]:
    """Remove thinking, redacted_thinking, and signature blocks."""
    return [b for b in content if b.get("type") not in _THINKING_BLOCK_TYPES]


# ── Step 6: Merge split messages ────────────────────────────────────

def merge_split_messages(messages: list[dict]) -> list[dict]:
    """Merge consecutive assistant messages that share the same message.id.

    Streaming parsers may produce multiple assistant messages per API response
    when parallel tool calls generate multiple content blocks. The API requires
    one message per response.
    """
    if not messages:
        return messages

    result: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        msg_id = msg.get("id")
        role = msg.get("role")

        # Only merge assistant messages with explicit IDs
        if role != "assistant" or not msg_id:
            result.append(msg)
            i += 1
            continue

        # Collect all consecutive assistant messages with the same id
        merged_content = list(msg.get("content", []))
        j = i + 1
        while j < len(messages):
            next_msg = messages[j]
            if (
                next_msg.get("role") == "assistant"
                and next_msg.get("id") == msg_id
            ):
                merged_content.extend(next_msg.get("content", []))
                j += 1
            else:
                break

        result.append({**msg, "content": merged_content})
        i = j

    return result


# ── Step 7: Fix tool pairings ───────────────────────────────────────

def fix_tool_pairings(messages: list[dict]) -> list[dict]:
    """Ensure every tool_use has a matching tool_result, and vice versa.

    Orphan fixes:
      - Orphan tool_use → inject synthetic error tool_result
      - Orphan tool_result → inject synthetic tool_use before it
    """
    # Collect all tool_use IDs and tool_result IDs
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()

    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    tool_use_ids.add(tid)
            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    tool_result_ids.add(tid)

    orphan_uses = tool_use_ids - tool_result_ids
    orphan_results = tool_result_ids - tool_use_ids

    if not orphan_uses and not orphan_results:
        return list(messages)  # Fast path: no orphans

    # Work on a mutable copy
    result = [{**m, "content": list(m.get("content", []))} for m in messages]

    # Fix orphan tool_use: inject synthetic tool_result in next user message
    for orphan_id in orphan_uses:
        # Find the message containing this tool_use
        use_idx = _find_tool_use_index(result, orphan_id)
        if use_idx is None:
            continue

        synthetic = {
            "type": "tool_result",
            "tool_use_id": orphan_id,
            "content": "Tool execution was interrupted or lost during session recovery.",
        }
        _inject_tool_result_after(result, use_idx, synthetic)

    # Fix orphan tool_result: inject synthetic tool_use in preceding assistant message
    for orphan_id in orphan_results:
        res_idx = _find_tool_result_index(result, orphan_id)
        if res_idx is None:
            continue

        synthetic = {
            "type": "tool_use",
            "id": orphan_id,
            "name": "unknown",
            "input": {},
        }
        _inject_tool_use_before(result, res_idx, synthetic)

    return result


def _find_tool_use_index(messages: list[dict], tool_id: str) -> int | None:
    """Find the index of the message containing a tool_use with the given id."""
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("id") == tool_id:
                    return i
    return None


def _find_tool_result_index(messages: list[dict], tool_id: str) -> int | None:
    """Find the index of the message containing a tool_result with the given id."""
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                    return i
    return None


def _inject_tool_result_after(messages: list[dict], idx: int, result_block: dict) -> None:
    """Inject a tool_result block after the assistant message's tool_use.

    If the next message is a user message, append to its content.
    Otherwise, insert a new user message with the result.
    """
    if idx + 1 < len(messages) and messages[idx + 1].get("role") == "user":
        user_msg = messages[idx + 1]
        # Mutate the copy we already made
        existing = user_msg.setdefault("content", [])
        existing.append(result_block)
    else:
        new_msg = {
            "role": "user",
            "content": [result_block],
        }
        messages.insert(idx + 1, new_msg)


def _inject_tool_use_before(messages: list[dict], idx: int, use_block: dict) -> None:
    """Inject a tool_use block into the preceding assistant message.

    If the message at idx-1 is an assistant message, prepend to its content.
    Otherwise, insert a new assistant message.
    """
    if idx > 0 and messages[idx - 1].get("role") == "assistant":
        asst_msg = messages[idx - 1]
        existing = asst_msg.setdefault("content", [])
        existing.insert(0, use_block)
    else:
        new_msg = {
            "role": "assistant",
            "content": [use_block],
        }
        messages.insert(idx, new_msg)


# ── Orchestrator ────────────────────────────────────────────────────

def normalize_messages(
    messages: list[dict],
    model_family: str,
    recent_errors: list[str] | None = None,
) -> list[dict]:
    """Run the full 7-step message normalization pipeline.

    Steps (fixed order):
      1. reorder_attachments    — bubble media blocks to front
      2. filter_virtual_messages — remove isVirtual=True
      3. build_error_block_map  — scan errors for strip targets
      4. strip_internal_elements — remove tool_reference, advisor, error-media
      5. handle_thinking_blocks — strip/preserve per model support
      6. merge_split_messages   — merge same-id assistant messages
      7. fix_tool_pairings      — repair orphan tool_use/tool_result

    Args:
        messages: Raw message list from AgentState.
        model_family: Model identifier (e.g. "deepseek-v4-pro").
        recent_errors: Recent API error strings for Step 3.

    Returns:
        New list of fully normalized messages, ready for the API.
    """
    if recent_errors is None:
        recent_errors = []

    # Step 1: Reorder attachments within messages
    messages = reorder_attachments(messages)

    # Step 2: Remove virtual messages
    messages = filter_virtual_messages(messages)

    # Step 3: Build error-to-block-type mapping from recent errors
    error_block_map = build_error_block_map(recent_errors)

    # Step 4: Strip internal elements and error-triggered media
    messages = strip_internal_elements(messages, error_block_map)

    # Step 5: Handle thinking/signature blocks per model
    messages = handle_thinking_blocks(messages, model_family)

    # Step 6: Merge split assistant messages
    messages = merge_split_messages(messages)

    # Step 7: Fix tool_use/tool_result pairings (strongest defense)
    messages = fix_tool_pairings(messages)

    return messages
