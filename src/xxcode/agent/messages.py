"""Message construction helpers for the core agent loop."""

from __future__ import annotations

import logging
from typing import Any

from ..tools import ToolCall
from .events import StreamEvent
from .state import AgentState

logger = logging.getLogger(__name__)


CONTINUATION_PROMPT = (
    "Your previous response was cut off due to output length limits. "
    "Please continue exactly where you left off. "
    "Do not repeat any content you already generated."
)


def add_usage(
    state: AgentState,
    input_tokens: int | dict[str, int] = 0,
    output_tokens: int = 0,
) -> None:
    """Accumulate token usage on the session state."""
    if isinstance(input_tokens, dict):
        usage = input_tokens
        state.total_input_tokens += int(usage.get("input_tokens", 0) or 0)
        state.total_output_tokens += int(usage.get("output_tokens", 0) or 0)
        state.cache_read_input_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
        state.cache_creation_input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        state.server_tool_use_input_tokens += int(usage.get("server_tool_use_input_tokens", 0) or 0)
        return

    if input_tokens:
        state.total_input_tokens += input_tokens
    if output_tokens:
        state.total_output_tokens += output_tokens


def _repair_orphan_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Defensively pair unpaired tool_use / tool_result blocks."""
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                tid = block.get("id", "")
                if tid:
                    tool_use_ids.add(tid)
            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if tid:
                    tool_result_ids.add(tid)

    orphan_uses = tool_use_ids - tool_result_ids
    orphan_results = tool_result_ids - tool_use_ids
    if not orphan_uses and not orphan_results:
        return messages

    repaired: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            repaired.append(msg)
            continue

        new_content: list[dict[str, Any]] = []
        has_orphan_use = False
        for block in content:
            if block.get("type") == "tool_result":
                if block.get("tool_use_id", "") in orphan_results:
                    continue
            elif block.get("type") == "tool_use":
                if block.get("id", "") in orphan_uses:
                    has_orphan_use = True
            new_content.append(block)

        if new_content:
            repaired.append({**msg, "content": new_content})

        if has_orphan_use:
            synthetic_results: list[dict[str, Any]] = []
            for block in content:
                if block.get("type") == "tool_use" and block.get("id", "") in orphan_uses:
                    synthetic_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": "[System: tool execution interrupted - no result available]",
                        }
                    )
            if synthetic_results:
                repaired.append({"role": "user", "content": synthetic_results})

    return repaired


def commit_assistant_turn(
    state: AgentState,
    *,
    thinking_content: list[dict[str, Any]],
    full_text: str,
    tool_calls: list[ToolCall],
    message_id: str | None,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record token usage and append the assistant message for one model turn."""
    add_usage(state, input_tokens, output_tokens)
    append_assistant_message(
        state,
        thinking_content=thinking_content,
        full_text=full_text,
        tool_calls=tool_calls,
        message_id=message_id,
    )


def build_assistant_message(
    *,
    thinking_content: list[dict[str, Any]],
    full_text: str,
    tool_calls: list[ToolCall],
    message_id: str | None,
) -> dict[str, Any]:
    """Build an Anthropic-compatible assistant message."""
    content: list[dict[str, Any]] = []
    content.extend(thinking_content)

    if full_text:
        content.append({"type": "text", "text": full_text})

    for tc in tool_calls:
        content.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.name,
            "input": tc.input,
        })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if message_id:
        message["id"] = message_id
    return message


def append_assistant_message(
    state: AgentState,
    *,
    thinking_content: list[dict[str, Any]],
    full_text: str,
    tool_calls: list[ToolCall],
    message_id: str | None,
) -> None:
    """Append a freshly-built assistant message to state."""
    state.messages.append(
        build_assistant_message(
            thinking_content=thinking_content,
            full_text=full_text,
            tool_calls=tool_calls,
            message_id=message_id,
        )
    )


def append_continuation_prompt(state: AgentState) -> None:
    """Ask the model to continue after a text-only truncated response."""
    state.messages.append({
        "role": "user",
        "content": [{"type": "text", "text": CONTINUATION_PROMPT}],
    })


def build_progress_event(progress: dict[str, Any]) -> StreamEvent:
    """Convert executor progress metadata into a stream event."""
    return StreamEvent(
        type="tool_progress",
        content=progress.get("chunk", ""),
        metadata={
            "tool_use_id": progress.get("tool_use_id", ""),
            "tool_name": progress.get("tool_name", ""),
        },
    )


def add_tool_failure_hint(
    state: AgentState,
    tool_results: list[dict[str, Any]],
    executor: Any,
) -> None:
    """Append a self-correction hint when a tool result failed."""
    if not tool_results:
        return

    has_error = False
    error_tool_name = ""
    for result in tool_results:
        tid = result.get("tool_use_id", "")
        slot = executor.get_slot(tid)
        if slot is not None and slot.is_error:
            has_error = True
            error_tool_name = slot.tc.name
            state.tool_errors[slot.tc.name] = (
                state.tool_errors.get(slot.tc.name, 0) + 1
            )
            break

    if not has_error:
        return

    error_count = state.tool_errors.get(error_tool_name, 1)
    if error_count >= 3:
        tool_results.append({
            "type": "text",
            "text": (
                "<system_hint>\n"
                f"The tool '{error_tool_name}' has failed {error_count} times "
                "in a row. DO NOT call it again with the same parameters.\n"
                "1. Carefully analyze the error message above to understand root cause.\n"
                "2. Pivot to a completely different approach or tool.\n"
                "3. If you believe the error is environmental, explain it to the user.\n"
                "</system_hint>"
            ),
        })
    else:
        tool_results.append({
            "type": "text",
            "text": (
                "<system_hint>\n"
                "One or more tool executions failed.\n"
                "1. Carefully analyze the error message above to understand what went wrong.\n"
                "2. DO NOT repeat the exact same tool call or command.\n"
                "3. Consider checking file paths, verifying syntax, or trying an alternative approach.\n"
                "</system_hint>"
            ),
        })


def commit_tool_results_turn(
    state: AgentState,
    tool_results: list[dict[str, Any]],
    executor: Any,
) -> None:
    """Attach failure hints and append tool results as a user turn."""
    add_tool_failure_hint(state, tool_results, executor)
    append_tool_results_message(state, tool_results)


def append_tool_results_message(
    state: AgentState,
    tool_results: list[dict[str, Any]],
) -> None:
    """Append deduplicated tool_result blocks as a user message."""
    if not tool_results:
        return

    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for result in tool_results:
        tid = result.get("tool_use_id", "")
        if tid and tid in seen_ids:
            logger.warning(
                "Duplicate tool_result blocked during injection: %s", tid,
            )
            continue
        if tid:
            seen_ids.add(tid)
        deduped.append(result)

    state.messages.append({
        "role": "user",
        "content": deduped,
    })
