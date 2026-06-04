"""Permission resolution for queued tool calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, AsyncGenerator
from typing import Any

from ..security.classifier import CommandClass, classify_command
from ..tools import ToolCall
from ..tools.registry import ToolRegistry
from .events import StreamEvent
from .state import AgentState
from .tools_executor import StreamingToolExecutor

logger = logging.getLogger(__name__)

PermissionFutureFactory = Callable[[], asyncio.Future]
PermissionFutureSetter = Callable[[asyncio.Future | None], None]
PermissionCheck = Callable[[ToolCall, Any, AgentState], bool]
PreToolHook = Callable[[ToolCall], Awaitable[None]]

PERMISSION_ONCE = "once"
PERMISSION_ALWAYS = "always"
PERMISSION_DENY = "deny"


def denied_tool_result_content(state: AgentState, tc: ToolCall) -> str:
    """Track a denied tool call and return the message to inject."""
    state.denied_tool_calls[tc.name] = (
        state.denied_tool_calls.get(tc.name, 0) + 1
    )
    denials = state.denied_tool_calls[tc.name]

    if denials < 3:
        return "User denied this action."

    return (
        "User denied this action.\n\n"
        "<system_hint>\n"
        f"The user has explicitly denied '{tc.name}' "
        f"{denials} times in a row. This action will NOT "
        "be approved on retry. DO NOT request it again. "
        "You MUST pivot to a completely different approach "
        "or explain to the user why this action is necessary "
        "and ask them to approve it directly.\n"
        "</system_hint>"
    )


class PermissionResolver:
    """Resolve permission prompts and start queued tool execution."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        check_permission: PermissionCheck,
        pre_tool_hook: PreToolHook,
        create_permission_future: PermissionFutureFactory,
        set_permission_future: PermissionFutureSetter,
    ):
        self._registry = registry
        self._check_permission = check_permission
        self._pre_tool_hook = pre_tool_hook
        self._create_permission_future = create_permission_future
        self._set_permission_future = set_permission_future

    async def resolve(
        self,
        *,
        tool_calls: list[ToolCall],
        executor: StreamingToolExecutor,
        state: AgentState,
        is_aborted: Callable[[], bool],
    ) -> AsyncGenerator[StreamEvent, None]:
        """Resolve queued tools and yield permission/denial events."""
        for tc in tool_calls:
            if is_aborted():
                if executor.is_queued(tc.id):
                    executor.deny_tool(tc.id)
                continue

            await self._pre_tool_hook(tc)

            if not executor.is_queued(tc.id):
                continue

            tool = self._registry.get(tc.name)
            if tool is None:
                executor.try_start_queued(tc.id)
                continue

            # Validate input early so is_read_only/is_destructive can be
            # input-aware (Insight 4.11.2).
            validated = self._validate_input(tool, tc)

            if not tool.is_read_only(validated):
                needs_perm = self._check_permission(tc, tool, state)
                risk = "normal"

                # Polymorphic: tools opt into command classification via
                # has_command_classifier() instead of a name check.
                if needs_perm and tool.has_command_classifier():
                    cmd = tc.input.get("command", "")
                    if cmd:
                        classification = classify_command(cmd)
                        if classification.command_class == CommandClass.SAFE:
                            needs_perm = False
                            logger.debug("Bash classifier auto-approved: %s", cmd[:80])
                        elif classification.command_class == CommandClass.DANGEROUS:
                            risk = "high"

                if risk == "normal" and tool.is_destructive(validated):
                    risk = "high"

                if needs_perm:
                    future = self._create_permission_future()
                    self._set_permission_future(future)
                    yield StreamEvent(
                        type="permission_needed",
                        content=tc.name,
                        metadata={
                            "tool_call": tc,
                            "risk": risk,
                            "dangerous": risk == "high",
                        },
                    )
                    decision, _ = await future
                    self._set_permission_future(None)

                    if decision in (PERMISSION_ONCE, PERMISSION_ALWAYS, True):
                        if decision == PERMISSION_ALWAYS:
                            self._persist_always_grant(state, tc, tool)
                        state.denied_tool_calls.pop(tc.name, None)
                        state.tool_errors.pop(tc.name, None)
                    else:
                        deny_content = denied_tool_result_content(state, tc)
                        executor.deny_tool(tc.id)
                        yield StreamEvent(
                            type="tool_result",
                            content=deny_content,
                            metadata={"tool_use_id": tc.id, "denied": True},
                        )
                        continue

            executor.try_start_queued(tc.id)

    @staticmethod
    def _persist_always_grant(state: AgentState, tc: ToolCall, tool: Any) -> None:
        """Persist only the narrow permission represented by an always grant."""
        if tc.name == "run_shell":
            command = tc.input.get("command", "")
            if not command:
                return
            try:
                from ..tools.BashTool.permissions import get_simple_command_prefix
            except Exception:
                return
            prefix = get_simple_command_prefix(command)
            if prefix:
                state.permission_state.confirm_command_prefix(prefix)
            return

        if tool.confirms_file_paths():
            file_path = tc.input.get("file_path")
            if file_path:
                state.permission_state.confirm_path(file_path)
            return

        state.permission_state.confirm_tool(tc.name)

    @staticmethod
    def _validate_input(tool: Any, tc: ToolCall) -> Any | None:
        """Validate raw input dict into a Pydantic model for input-aware checks.

        Returns the validated model or None on failure (caller handles fallback).
        """
        try:
            return tool.input_schema.model_validate(tc.input)
        except Exception:
            return None
