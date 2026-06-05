"""SubAgent - lightweight isolated agent for delegated tasks.

A SubAgent runs a single prompt with a filtered tool set, no permission
prompts (inheriting the parent's grants), and a hard turn limit. It is
spawned by AgentTool and runs inside the parent's async context.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..api.client import APIClient
from ..api.retry import RetryConfig
from ..config import Config
from ..context.builder import (
    assemble_prompt_sections,
    build_subagent_prompt_sections,
    get_git_context,
)
from ..skills.runtime import collect_inline_skill_runtime
from ..tools import ToolCall
from ..tools.registry import ToolRegistry
from .state import AgentState
from .recall_utils import (
    clip_recall_text,
    format_tool_input_for_recall,
    get_recent_tool_names,
    is_read_like_tool,
)

logger = logging.getLogger(__name__)

MAX_SUBAGENT_TURNS = 50
MAX_CONTINUATION_RETRIES = 2

_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "length", "token_limit"})
_READ_LIKE_TOOL_NAMES = frozenset({
    "read_file",
    "grep_search",
    "glob_match",
    "search",
    "tool_search",
})
@dataclass
class SubAgentSessionState:
    """Request-scoped execution state for one sub-agent request."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    surfaced_memory_ids: set[str] = field(default_factory=set)
    recent_tool_observations: list[dict[str, Any]] = field(default_factory=list)
    total_tool_use_count: int = 0
    scope_id: str = ""
    current_task_id: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    abort_check: Callable[[], bool] = field(default_factory=lambda: lambda: False)


@dataclass
class SubAgentRequestResult:
    """Final result for a single sub-agent request."""

    final_text: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class SubAgent:
    """A lightweight agent that runs one task with constrained tools."""

    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        definition: Any,
        parent_state: AgentState | None = None,
        max_turns: int | None = None,
        model_override: str | None = None,
        thinking_budget_tokens: int | None = None,
        agent_type: str | None = None,
        extra_context: dict[str, Any] | None = None,
        system_prompt_override: str | None = None,
    ):
        self._config = config
        self._registry = registry
        self._definition = definition
        self._parent_state = parent_state
        self._max_turns = max_turns or getattr(definition, "max_turns", MAX_SUBAGENT_TURNS)
        self._aborted = False
        self._extra_context = extra_context or {}
        self._worktree_cwd: Path | None = None
        wt_cwd = self._extra_context.get("worktree_cwd")
        if wt_cwd is not None:
            self._worktree_cwd = Path(wt_cwd)
        self._agent_type = agent_type or getattr(definition, "name", "general-purpose")
        self._system_prompt_override = system_prompt_override
        self._thinking_budget_tokens = thinking_budget_tokens
        self._model = model_override or definition.model or config.api_model
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def abort(self) -> None:
        """Signal the sub-agent to stop."""
        self._aborted = True

    async def run(self, prompt: str) -> str:
        """Execute one sub-agent request and return the final result text."""
        session_state = await self._create_session_state(prompt)
        result = await self._execute_one_request(prompt, session_state)
        self._total_input_tokens += result.total_input_tokens
        self._total_output_tokens += result.total_output_tokens
        return result.final_text

    async def _create_session_state(self, prompt: str) -> SubAgentSessionState:
        """Build a fresh request-scoped state object."""
        system_prompt = await self._build_system_prompt()
        messages = await self._build_initial_messages(prompt)
        abort_check = self._extra_context.get("abort_check")
        if not callable(abort_check):
            abort_check = None
        return SubAgentSessionState(
            messages=messages,
            system_prompt=system_prompt,
            surfaced_memory_ids=self._get_surfaced_agent_memory_ids(messages),
            scope_id=str(self._extra_context.get("scope_id", "") or ""),
            current_task_id=str(self._extra_context.get("current_task_id", "") or ""),
            abort_check=abort_check or (lambda: False),
        )

    async def _execute_one_request(
        self,
        prompt: str,
        session_state: SubAgentSessionState,
    ) -> SubAgentRequestResult:
        """Execute one request without waiting for any future messages."""
        messages = session_state.messages
        system_prompt = session_state.system_prompt
        surfaced_memory_ids = set(session_state.surfaced_memory_ids)
        continuation_retries = 0
        should_drain_notifications = True
        full_text = ""

        for _turn in range(self._max_turns):
            if self._aborted or session_state.abort_check():
                return SubAgentRequestResult(
                    final_text="Sub-agent aborted by user.",
                    total_input_tokens=session_state.total_input_tokens,
                    total_output_tokens=session_state.total_output_tokens,
                )

            if should_drain_notifications:
                await self._drain_pending_notifications(session_state)
            should_drain_notifications = True

            runtime = collect_inline_skill_runtime(messages)
            active_registry = self._registry
            if runtime.allowed_tool_names is not None:
                active_registry = self._registry.filtered_copy(
                    allow_list=set(runtime.allowed_tool_names)
                )
            tool_schemas = active_registry.get_api_schemas()

            client = APIClient(
                api_key=self._config.api_key,
                base_url=self._config.api_base_url,
                model=self._model,
                max_tokens=self._config.api_max_tokens,
                thinking_budget_tokens=self._thinking_budget_tokens,
                retry_config=RetryConfig(),
            )

            tool_calls: list[ToolCall] = []
            full_text = ""
            thinking_blocks: list[dict[str, Any]] = []
            current_message_id: str | None = None
            output_truncated = False

            try:
                api_messages = self._prepare_api_messages(messages)
                async for event in client.stream_chat(
                    system_prompt=system_prompt,
                    messages=api_messages,
                    tools=tool_schemas,
                ):
                    event_type = event["type"]
                    if event_type == "text_delta":
                        full_text += event["text"]
                    elif event_type == "tool_use":
                        tool_calls.append(
                            ToolCall(
                                id=event["id"],
                                name=event["name"],
                                input=event["input"],
                            )
                        )
                    elif event_type == "thinking":
                        thinking_blocks.append({
                            "type": "thinking",
                            "thinking": event["text"],
                            "signature": event.get("signature", ""),
                        })
                    elif event_type == "redacted_thinking":
                        thinking_blocks.append({
                            "type": "redacted_thinking",
                            "data": event.get("data", ""),
                        })
                    elif event_type == "thinking_delta":
                        if thinking_blocks and thinking_blocks[-1].get("type") == "thinking":
                            thinking_blocks[-1]["thinking"] += event.get("text", "")
                    elif event_type == "signature_delta":
                        if thinking_blocks and thinking_blocks[-1].get("type") == "thinking":
                            existing = thinking_blocks[-1].get("signature", "")
                            thinking_blocks[-1]["signature"] = existing + event.get("signature", "")
                    elif event_type == "message_id":
                        current_message_id = event["id"]
                    elif event_type == "stop_reason":
                        reason = event.get("stop_reason", event.get("reason", ""))
                        if reason in _TRUNCATED_STOP_REASONS:
                            output_truncated = True
                            logger.debug("Sub-agent output truncated: stop_reason=%s", reason)
                    elif event_type == "usage":
                        session_state.total_input_tokens += event.get("input_tokens", 0)
                        session_state.total_output_tokens += event.get("output_tokens", 0)
                    elif event_type == "error":
                        msg = event.get("message", "Unknown API error")
                        logger.warning("Sub-agent API error: %s", msg[:200])
                        return SubAgentRequestResult(
                            final_text=f"Sub-agent error: {msg[:500]}",
                            total_input_tokens=session_state.total_input_tokens,
                            total_output_tokens=session_state.total_output_tokens,
                        )
            except Exception as exc:
                logger.exception("Sub-agent API call failed")
                return SubAgentRequestResult(
                    final_text=f"Sub-agent API call failed: {exc}",
                    total_input_tokens=session_state.total_input_tokens,
                    total_output_tokens=session_state.total_output_tokens,
                )

            assistant_content: list[dict[str, Any]] = []
            assistant_content.extend(thinking_blocks)
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
            if current_message_id:
                assistant_msg["id"] = current_message_id
            messages.append(assistant_msg)

            if output_truncated and continuation_retries < MAX_CONTINUATION_RETRIES:
                continuation_retries += 1
                logger.info(
                    "Sub-agent output truncated - injecting continuation (attempt %d/%d).",
                    continuation_retries,
                    MAX_CONTINUATION_RETRIES,
                )
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "Your previous response was cut off due to output "
                            "length limits. Please continue exactly where you "
                            "left off. Do not repeat any content you already "
                            "generated."
                        ),
                    }],
                })
                should_drain_notifications = False
                continue

            if not tool_calls:
                return SubAgentRequestResult(
                    final_text=full_text if full_text else "(Sub-agent produced no output.)",
                    total_input_tokens=session_state.total_input_tokens,
                    total_output_tokens=session_state.total_output_tokens,
                )

            exec_ctx: dict[str, Any] = {
                "cwd": str(self._worktree_cwd or self._config.cwd),
                "config": self._config,
                "allowed_read_roots": [str(self._config.cwd)],
                "parent_state": self._parent_state,
                "_registry": active_registry,
                "_pending_skill_messages": [],
                **self._extra_context,
            }

            tool_results: list[dict[str, Any]] = []
            tool_observations: list[dict[str, Any]] = []
            for tc in tool_calls:
                result = await active_registry.execute(tc, exec_ctx)
                tool = active_registry.get(tc.name)
                if tool is not None and not result.is_error:
                    content = await tool.format_large_result(
                        content=result.content,
                        max_chars=self._config.max_tool_output_chars,
                        tool_use_id=tc.id,
                        session_dir=str(self._config.session_dir),
                    )
                else:
                    content = result.content

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": content,
                })
                tool_observations.append({
                    "call": tc,
                    "result": result,
                    "tool": tool,
                    "content": content,
                })
            session_state.recent_tool_observations = tool_observations
            session_state.total_tool_use_count += len(tool_observations)

            continuation_retries = 0

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                pending_skill_messages = exec_ctx.get("_pending_skill_messages", [])
                if pending_skill_messages:
                    messages.extend(pending_skill_messages)
                    pending_skill_messages.clear()
                surfaced_memory_ids = await self._append_fresh_agent_memories(
                    messages,
                    task_prompt=prompt,
                    tool_observations=tool_observations,
                    surfaced_memory_ids=surfaced_memory_ids,
                )
                session_state.surfaced_memory_ids = surfaced_memory_ids

        return SubAgentRequestResult(
            final_text=(
                f"Sub-agent reached maximum turns ({self._max_turns}). "
                f"Last response: {full_text[:500] if full_text else '(none)'}"
            ),
            total_input_tokens=session_state.total_input_tokens,
            total_output_tokens=session_state.total_output_tokens,
        )

    async def _drain_pending_notifications(
        self,
        session_state: SubAgentSessionState,
    ) -> None:
        """Inject scope-local notifications at the top of a new outer turn."""
        drain = self._extra_context.get("_drain_pending_notifications")
        if not callable(drain):
            return
        drained = drain(
            scope_id=session_state.scope_id,
            current_task_id=session_state.current_task_id,
        )
        if inspect.isawaitable(drained):
            drained = await drained
        if isinstance(drained, list) and drained:
            session_state.messages.extend(drained)

    async def _build_system_prompt(self) -> str:
        """Build the system prompt for this sub-agent."""
        if self._system_prompt_override:
            return self._system_prompt_override

        cwd = self._worktree_cwd or self._config.cwd or "."
        cwd_path = Path(cwd)

        git_info = get_git_context(cwd_path, compact=True)

        agent_memory = ""
        if getattr(self._config, "auto_memory_enabled", True):
            try:
                from ..memory.agent_memory import build_agent_memory_prompt

                agent_memory = build_agent_memory_prompt(
                    self._agent_type,
                    self._resolve_project_root(),
                )
            except Exception:
                logger.debug("Agent memory prompt build failed", exc_info=True)

        sections = build_subagent_prompt_sections(
            agent_name=self._definition.name,
            description=self._definition.description,
            cwd=cwd_path,
            max_turns=self._max_turns,
            git_context=git_info,
            agent_memory=agent_memory,
        )
        return assemble_prompt_sections(sections)

    async def _build_initial_messages(self, prompt: str) -> list[dict[str, Any]]:
        """Build hidden memory context followed by the actual sub-agent task."""
        messages: list[dict[str, Any]] = []
        if getattr(self._config, "auto_memory_enabled", True):
            try:
                from ..memory.agent_memory import (
                    build_agent_memory_context_messages,
                    build_recalled_agent_memories_message,
                    recall_agent_memories_for_query,
                )

                messages.extend(
                    build_agent_memory_context_messages(
                        self._agent_type,
                        self._resolve_project_root(),
                    )
                )

                recalled = await recall_agent_memories_for_query(
                    self._agent_type,
                    self._resolve_project_root(),
                    prompt,
                    client_factory=self._recall_client_factory,
                )
                recalled_msg = build_recalled_agent_memories_message(
                    recalled,
                    source="agent_memory_recall",
                )
                if recalled_msg is not None:
                    messages.append(recalled_msg)
            except Exception:
                logger.debug("Agent memory context build failed", exc_info=True)

        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        })
        return messages

    async def _append_fresh_agent_memories(
        self,
        messages: list[dict[str, Any]],
        *,
        task_prompt: str,
        tool_observations: list[dict[str, Any]],
        surfaced_memory_ids: set[str],
    ) -> set[str]:
        """Append newly recalled agent memories without mutating prior history."""
        if not getattr(self._config, "auto_memory_enabled", True):
            return surfaced_memory_ids
        if not self._should_trigger_agent_memory_recall(tool_observations):
            return surfaced_memory_ids

        recall_query = self._build_agent_memory_recall_query(
            task_prompt,
            tool_observations,
        )
        if not recall_query:
            return surfaced_memory_ids

        try:
            from ..memory.agent_memory import (
                build_recalled_agent_memories_message,
                recall_agent_memories_for_query,
            )

            recalled = await recall_agent_memories_for_query(
                self._agent_type,
                self._resolve_project_root(),
                recall_query,
                client_factory=self._recall_client_factory,
                recent_tools=self._get_recent_tool_names(messages),
                already_surfaced=surfaced_memory_ids,
            )
            recalled_msg = build_recalled_agent_memories_message(
                recalled,
                source="agent_memory_recall",
            )
            if recalled_msg is None:
                return surfaced_memory_ids

            messages.append(recalled_msg)
            updated = set(surfaced_memory_ids)
            for memory in recalled:
                memory_id = getattr(memory, "recall_id", None) or memory.filename
                if memory_id.endswith(".md"):
                    updated.add(memory_id)
            return updated
        except Exception:
            logger.debug("Agent memory recall append failed", exc_info=True)
            return surfaced_memory_ids

    @staticmethod
    def _should_trigger_agent_memory_recall(tool_observations: list[dict[str, Any]]) -> bool:
        """Trigger recall for fresh errors or new repo-reading observations."""
        if not tool_observations:
            return False
        for observation in tool_observations:
            result = observation["result"]
            if getattr(result, "is_error", False):
                return True
        return any(
            SubAgent._is_read_like_tool(
                observation["call"].name,
                observation.get("tool"),
                observation["call"].input,
            )
            for observation in tool_observations
        )

    @staticmethod
    def _is_read_like_tool(
        name: str,
        tool: Any,
        raw_input: dict[str, Any] | None = None,
    ) -> bool:
        return is_read_like_tool(
            name,
            tool,
            raw_input,
            read_like_names=_READ_LIKE_TOOL_NAMES,
        )

    def _build_agent_memory_recall_query(
        self,
        task_prompt: str,
        tool_observations: list[dict[str, Any]],
    ) -> str:
        """Build a concise recall query from the task and latest tool outcomes."""
        errors: list[str] = []
        observations: list[str] = []

        for observation in tool_observations:
            call = observation["call"]
            result = observation["result"]
            content = clip_recall_text(observation["content"])
            details = format_tool_input_for_recall(call.input)
            label = call.name if not details else f"{call.name} ({details})"
            line = f"- {label}: {content}"
            if getattr(result, "is_error", False):
                errors.append(line)
            elif is_read_like_tool(call.name, observation.get("tool"), call.input):
                observations.append(line)

        parts = [f"Task: {task_prompt}"]
        if errors:
            parts.extend(["", "Recent tool errors:", *errors[:3]])
        elif observations:
            parts.extend(["", "Recent observations:", *observations[:3]])
        return "\n".join(parts)

    @staticmethod
    def _format_tool_input_for_recall(raw_input: dict[str, Any]) -> str:
        """Extract the most useful location hints from a tool call input."""
        return format_tool_input_for_recall(raw_input)

    @staticmethod
    def _clip_recall_text(text: str, *, limit: int = 400) -> str:
        return clip_recall_text(text, limit=limit)

    @staticmethod
    def _get_recent_tool_names(messages: list[dict[str, Any]]) -> list[str]:
        """Extract recently used tool names from recent assistant turns."""
        return get_recent_tool_names(messages)

    @staticmethod
    def _get_surfaced_agent_memory_ids(messages: list[dict[str, Any]]) -> set[str]:
        """Recover already-appended agent-memory ids from message metadata."""
        try:
            from ..memory.injection import recalled_memory_ids

            return recalled_memory_ids(messages, source="agent_memory_recall")
        except Exception:
            logger.debug("Unable to read prior agent-memory recall ids", exc_info=True)
            return set()

    @property
    def tokens_used(self) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) consumed by the sub-agent."""
        return self._total_input_tokens, self._total_output_tokens

    def _prepare_api_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove internal-only metadata before sending messages to the API."""
        cleaned_messages: list[dict[str, Any]] = []
        for msg in messages:
            cleaned_messages.append({
                key: value
                for key, value in msg.items()
                if key not in {"metadata", "isMeta"}
            })
        return cleaned_messages

    def _resolve_project_root(self) -> Path:
        from ..memory.agent_memory import resolve_agent_memory_project_root

        return resolve_agent_memory_project_root(Path(self._config.cwd or "."))

    async def _recall_client_factory(self):
        return APIClient(
            api_key=self._config.api_key,
            base_url=self._config.api_base_url,
            model=self._model,
            max_tokens=256,
            retry_config=RetryConfig(),
        )
