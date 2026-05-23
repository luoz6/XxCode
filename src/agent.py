"""Core Coding Agent — the main tool-loop controller.

The agent runs a "think → act → observe" loop:
1. Send conversation to the model (streaming)
2. If model returns text only → task complete, exit
3. If model calls tools → execute each tool → inject results → loop back
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api.client import APIClient
from .api.retry import RetryConfig
from .compact.truncate import truncate_result
from .config import Config, get_config
from .prompt import build_system_prompt
from .security.permission import PermissionState, needs_user_permission
from .tools import ToolCall, ToolResult
from .tools.file_edit import EditFileTool
from .tools.file_read import ReadFileTool
from .tools.file_write import WriteFileTool
from .tools.glob_match import GlobMatchTool
from .tools.grep_search import GrepSearchTool
from .tools.registry import ToolRegistry
from .tools.shell import RunShellTool

logger = logging.getLogger(__name__)

# ── Events ──────────────────────────────────────────────────────────


@dataclass
class StreamEvent:
    """One event from the agent's streaming output."""
    type: str  # "text", "tool_call", "tool_result", "thinking", "error", "cost", "done"
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── State ───────────────────────────────────────────────────────────


@dataclass
class AgentState:
    """Concentrated agent state. Updated immutably via replace()."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_count: int = 0

    # Permission tracking
    permission_state: PermissionState = field(default_factory=PermissionState)

    # Session
    system_prompt: str = ""


# ── Agent ───────────────────────────────────────────────────────────


class CodingAgent:
    """The main coding agent. Runs the tool-loop until the model stops calling tools."""

    def __init__(self, config: Config | None = None):
        self.config = config or get_config()
        self._aborted = False
        self._registry = ToolRegistry()
        self._register_default_tools()
        self._last_state: AgentState | None = None

    def _register_default_tools(self) -> None:
        """Register the 6 built-in tools."""
        self._registry.register(ReadFileTool())
        self._registry.register(WriteFileTool())
        self._registry.register(EditFileTool())
        self._registry.register(GrepSearchTool())
        self._registry.register(GlobMatchTool())
        self._registry.register(RunShellTool())

    def abort(self) -> None:
        """Signal the agent to stop after the current turn."""
        self._aborted = True

    def reset(self) -> None:
        """Reset agent for a new conversation."""
        self._aborted = False

    # ── Public API ────────────────────────────────────────────────

    async def chat(
        self,
        user_prompt: str,
        state: AgentState | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run the agent on a user prompt.

        Args:
            user_prompt: The user's message.
            state: Existing state to resume from (creates new state if None).

        Yields:
            StreamEvent objects for the UI to render.
        """
        if state is None:
            state = AgentState()
            state.system_prompt = build_system_prompt(self.config.cwd)

        # Add user message
        user_msg: dict[str, Any] = {
            "role": "user",
            "content": [{"type": "text", "text": user_prompt}],
        }
        state.messages.append(user_msg)

        while True:
            if self._aborted:
                self._last_state = state
                yield StreamEvent(type="done", content="Aborted by user.")
                break

            # Check if we need to compress
            if self._should_compress(state):
                state = await self._compact(state)

            # Call the model
            tool_calls: list[ToolCall] = []
            full_text = ""
            input_tokens = 0
            output_tokens = 0

            try:
                client = self._build_client()

                all_messages = self._build_messages(state)
                tool_schemas = self._registry.get_api_schemas()

                async for event in client.stream_chat(
                    system_prompt=state.system_prompt,
                    messages=all_messages,
                    tools=tool_schemas,
                ):
                    if event["type"] == "text_delta":
                        full_text += event["text"]
                        yield StreamEvent(type="text", content=event["text"])

                    elif event["type"] == "tool_use":
                        tc = ToolCall(
                            id=event["id"],
                            name=event["name"],
                            input=event["input"],
                        )
                        tool_calls.append(tc)
                        yield StreamEvent(
                            type="tool_call",
                            content=event["name"],
                            metadata={"id": event["id"], "input": event["input"]},
                        )

                    elif event["type"] == "usage":
                        input_tokens = event.get("input_tokens", 0)
                        output_tokens = event.get("output_tokens", 0)

                    elif event["type"] == "error":
                        msg = event.get("message", "Unknown API error")
                        yield StreamEvent(type="error", content=msg)
                        # If we got an error but no tool calls, exit
                        if not tool_calls:
                            self._last_state = state
                            yield StreamEvent(type="done", content="")
                            return
                        break

            except Exception as e:
                logger.exception("API call failed")
                self._last_state = state
                yield StreamEvent(type="error", content=f"API error: {e}")
                yield StreamEvent(type="done", content="")
                return

            # Update token counts
            if input_tokens:
                state.total_input_tokens += input_tokens
            if output_tokens:
                state.total_output_tokens += output_tokens

            # Build assistant message
            assistant_content: list[dict[str, Any]] = []

            if full_text:
                assistant_content.append({"type": "text", "text": full_text})

            for tc in tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_content,
            }
            state.messages.append(assistant_msg)

            # No tool calls → task complete
            if not tool_calls:
                cost = self._calculate_cost(state)
                self._last_state = state
                yield StreamEvent(type="cost", content=f"${cost:.4f}", metadata={"cost": cost})
                yield StreamEvent(type="done", content="")
                break

            # Execute tools
            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                if self._aborted:
                    break

                # Permission check
                tool = self._registry.get(tc.name)
                if tool is not None and not tool.is_read_only():
                    if self._needs_permission_for_call(tc, state):
                        yield StreamEvent(
                            type="permission_needed",
                            content=tc.name,
                            metadata={"tool_call": tc},
                        )
                        # We need user input here — handled by the CLI layer
                        # The CLI sets confirmation before resuming
                        if not state.permission_state.is_tool_confirmed(tc.name):
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": "User denied this action.",
                            })
                            yield StreamEvent(
                                type="tool_result",
                                content="Denied",
                                metadata={"tool_use_id": tc.id, "denied": True},
                            )
                            continue

                # Execute
                context = {"cwd": str(self.config.cwd), "config": self.config}
                result = await self._registry.execute(tc, context)
                truncated = truncate_result(result.content, self.config.max_tool_output_chars)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": truncated,
                })

                yield StreamEvent(
                    type="tool_result",
                    content="",
                    metadata={
                        "tool_use_id": tc.id,
                        "tool_name": tc.name,
                        "result": truncated[:500],  # Show truncated in UI
                        "is_error": result.is_error,
                    },
                )

            # Inject tool results
            if tool_results:
                state.messages.append({
                    "role": "user",
                    "content": tool_results,
                })

            state.turn_count += 1

            # Auto-approve paths that were confirmed
            for tc in tool_calls:
                if state.permission_state.is_tool_confirmed(tc.name):
                    file_path = tc.input.get("file_path")
                    if file_path:
                        state.permission_state.confirm_path(file_path)

    # ── Helpers ────────────────────────────────────────────────────

    def _build_client(self) -> APIClient:
        return APIClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.api_model,
            max_tokens=self.config.api_max_tokens,
            retry_config=RetryConfig(),
        )

    def _build_messages(self, state: AgentState) -> list[dict[str, Any]]:
        """Build messages list for the API from agent state.

        Converts our internal format to what the Anthropic API expects.
        """
        return list(state.messages)

    def _should_compress(self, state: AgentState) -> bool:
        """Check if total tokens exceed the compression threshold."""
        # Rough estimate: 1 token ≈ 4 chars
        estimated_tokens = sum(
            len(str(m.get("content", ""))) // 4 for m in state.messages
        )
        # Assume 200K context window, trigger at 85%
        context_limit = 200_000
        return estimated_tokens > int(context_limit * self.config.context_compress_threshold)

    async def _compact(self, state: AgentState) -> AgentState:
        """Basic compaction: drop oldest user-assistant pairs, keeping system context."""
        # Keep first message (system context) and last 10 exchanges
        if len(state.messages) > 20:
            # Find tool result messages and assistant messages
            keep_last = 20
            state.messages = state.messages[:1] + state.messages[-keep_last:]
        return state

    def _calculate_cost(self, state: AgentState) -> float:
        """Calculate approximate cost based on token usage."""
        # Anthropic pricing (approximate, varies by model)
        input_price_per_1k = 0.003   # $3/MTok
        output_price_per_1k = 0.015  # $15/MTok

        input_cost = (state.total_input_tokens / 1000) * input_price_per_1k
        output_cost = (state.total_output_tokens / 1000) * output_price_per_1k
        return input_cost + output_cost

    def _needs_permission_for_call(self, tc: ToolCall, state: AgentState) -> bool:
        """Check if a given tool call needs user permission."""
        tool = self._registry.get(tc.name)
        if tool is None:
            return True
        if tool.is_read_only():
            return False
        if state.permission_state.yolo_mode:
            return False
        if state.permission_state.is_tool_confirmed(tc.name):
            return False

        # Create a pydantic input to check
        try:
            validated_input = tool.input_schema.model_validate(tc.input)
        except Exception:
            return True

        return needs_user_permission(tc.name, validated_input, state.permission_state)


# ── Factory ─────────────────────────────────────────────────────────


def create_agent(config: Config | None = None) -> CodingAgent:
    """Create a CodingAgent with all default tools registered."""
    return CodingAgent(config=config)
