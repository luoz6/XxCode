"""CoreExecutionEngine - the inner tool-loop controller.

This module keeps the loop focused on orchestration:
1. stream model output
2. handle PTL / output truncation recovery
3. execute tools
4. resolve permissions
5. inject assistant / tool_result messages

The heavy lifting lives in helper modules:
- messages.py
- output_recovery.py
- ptl_recovery.py
- permission_resolver.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from ..api.client import APIClient
from ..api.retry import RetryConfig
from ..config import Config, get_config
from ..memory.injection import (
    MEMORY_INDEX_SOURCE,
    build_memory_index_message,
    build_recalled_memories_message,
    recalled_memory_ids,
    strip_memory_context_messages,
)
from ..memory.agent_memory import recall_agent_memories_for_query
from ..memory.recall import MemoryRecall, recall_memories_for_query
from ..skills import (
    EFFORT_THINKING_BUDGETS,
    SKILL_LISTING_SOURCE,
    SKILL_RECOVERY_SOURCE,
    resolve_skill_context_cwd,
    SkillDiscovery,
    SkillExecutor,
    SkillPersistence,
    SkillRegistry,
    SkillTool,
    collect_inline_skill_runtime,
    strip_skill_context_messages,
)
from ..tools import ToolCall
from ..tools.file_edit import EditFileTool
from ..tools.file_read import ReadFileTool
from ..tools.file_write import WriteFileTool
from ..tools.glob_match import GlobMatchTool
from ..tools.grep_search import GrepSearchTool
from ..tools.notebook_edit import NotebookEditTool
from ..tools.registry import ToolRegistry
from ..tools.search import ToolSearchTool
from ..tools.tasks import (
    SendMessageTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
    TaskWaitTool,
)
from .events import StreamEvent
from .messages import (
    build_progress_event,
    commit_assistant_turn,
    commit_tool_results_turn,
)
from .output_recovery import (
    OutputRecoveryState,
    handle_output_truncation,
    is_output_truncated_stop_reason,
)
from .permission_resolver import PermissionResolver
from .ptl_recovery import PTLRecoveryManager, is_ptl_error_message
from .state import AgentState
from .task_runtime import AgentTaskRuntime
from .tools_executor import StreamingToolExecutor

logger = logging.getLogger(__name__)

MAX_API_ERROR_RETRIES = 3
MAX_PARENT_TURNS = 100
_FATAL_API_ERROR_KEYWORDS = (
    "404",
    "model_not_found",
    "model not found",
    "invalid_api_key",
    "invalid api key",
    "authentication",
    "authorization",
    "not_found",
    "not found",
)
_STREAM_ACTION_CONTINUE = "continue"
_STREAM_ACTION_BREAK = "break"
_STREAM_ACTION_RETURN = "return"
_READ_LIKE_TOOL_NAMES = frozenset({
    "read_file",
    "grep_search",
    "glob_match",
})


def _is_fatal_api_error(msg: str) -> bool:
    low = msg.lower()
    return any(keyword in low for keyword in _FATAL_API_ERROR_KEYWORDS)


def _consume_task_exception(task: asyncio.Task) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _cancel_recall_prefetch(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@dataclass
class ModelTurn:
    """Collected state from one model streaming turn."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking_content: list[dict[str, Any]] = field(default_factory=list)
    full_text: str = ""
    current_message_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    output_truncated: bool = False
    ptl_detected: bool = False
    ptl_error_msg: str = ""
    stream_had_error: bool = False


@dataclass
class StreamAction:
    """Decision from handling one raw API stream event."""

    action: str = _STREAM_ACTION_CONTINUE
    event: StreamEvent | None = None
    consecutive_api_errors: int = 0


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


class CoreExecutionEngine:
    """Inner execution engine - runs the tool-loop until the model stops calling tools."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        skill_registry: SkillRegistry | None = None,
        skill_executor: SkillExecutor | None = None,
    ):
        self.config = config or get_config()
        self._aborted = False
        self._registry = ToolRegistry()
        self._register_default_tools()
        self._last_state: AgentState | None = None
        self._permission_future: asyncio.Future | None = None
        self._skill_permission_future: asyncio.Future | None = None
        self._mcp_trust_future: asyncio.Future | None = None
        self._l3_regions: list[Any] = []
        self._context: dict[str, Any] = {}
        self._mcp_initialized = False
        self._skill_permission_events: list[StreamEvent] = []
        self._skill_recovery_ready: dict[str, bool] = {}
        self.task_runtime = AgentTaskRuntime()
        self._skill_registry = skill_registry
        self._skill_executor = skill_executor
        self._context.update(
            {
                "task_runtime": self.task_runtime,
                "scope_id": "main",
                "current_task_id": None,
                "parent_task_id": None,
                "parent_scope_id": None,
                "_drain_pending_notifications": self.task_runtime.drain_pending_notifications,
            }
        )
        self._skill_discovery = (
            SkillDiscovery(skill_registry)
            if self.config.skills_enabled and skill_registry is not None
            else None
        )
        self._skill_persistence = (
            SkillPersistence()
            if self.config.skills_enabled and skill_registry is not None
            else None
        )
        if (
            self.config.skills_enabled
            and skill_registry is not None
            and skill_executor is not None
        ):
            self._registry.register(SkillTool(skill_registry, skill_executor))

        from ..memory.extraction import ExtractionController
        self._extraction_controller = ExtractionController(self.config, self._registry)

    def _register_default_tools(self) -> None:
        from ..tools.BashTool import BashTool as _BashTool
        from ..tools.agent import AgentTool as _AgentTool

        self._registry.register_class(ReadFileTool)
        self._registry.register_class(WriteFileTool)
        self._registry.register_class(EditFileTool)
        self._registry.register_class(GrepSearchTool)
        self._registry.register_class(GlobMatchTool)
        self._registry.register_class(
            NotebookEditTool,
            should_defer=True,
            search_hint="notebook jupyter ipynb cell edit",
        )
        self._registry.register_class(_BashTool)
        self._registry.register_class(_AgentTool)
        self._registry.register_class(TaskListTool)
        self._registry.register_class(TaskGetTool)
        self._registry.register_class(TaskWaitTool)
        self._registry.register_class(TaskStopTool)
        self._registry.register_class(SendMessageTool)
        self._registry.register_class(ToolSearchTool)

    def abort(self) -> None:
        self._aborted = True
        self._cancel_permission()

    def reset(self) -> None:
        self._aborted = False
        self._extraction_controller.cancel()
        self._skill_recovery_ready.clear()
        self.task_runtime = AgentTaskRuntime()
        self._context.update(
            {
                "task_runtime": self.task_runtime,
                "scope_id": "main",
                "current_task_id": None,
                "parent_task_id": None,
                "parent_scope_id": None,
                "_drain_pending_notifications": self.task_runtime.drain_pending_notifications,
            }
        )
        if self._skill_persistence is not None:
            self._skill_persistence.clear_all()

    async def shutdown(self) -> None:
        """Full teardown: disconnect MCP clients, reset state.

        Call on session exit or /quit so stdio MCP subprocesses are
        terminated rather than left as orphans.
        """
        await self._disconnect_mcp()
        await self.task_runtime.shutdown()
        self._aborted = False
        self._mcp_initialized = False
        self._extraction_controller.cancel()
        self._skill_recovery_ready.clear()
        self.task_runtime = AgentTaskRuntime()
        self._context.update(
            {
                "task_runtime": self.task_runtime,
                "scope_id": "main",
                "current_task_id": None,
                "parent_task_id": None,
                "parent_scope_id": None,
                "_drain_pending_notifications": self.task_runtime.drain_pending_notifications,
            }
        )
        if self._skill_persistence is not None:
            self._skill_persistence.clear_all()

    async def clear_mcp(self) -> None:
        """Disconnect MCP clients and mark for re-initialisation.

        Suitable for /clear — the next query loop will re-discover
        MCP tools from fresh connections.
        """
        await self._disconnect_mcp()
        self._mcp_initialized = False

    async def _disconnect_mcp(self) -> None:
        """Disconnect all MCP clients registered in the execution context."""
        clients: dict = self._context.get("mcp_clients", {})
        if not clients:
            return
        logger.info("Disconnecting %d MCP client(s)...", len(clients))
        for name, client in list(clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:
                logger.warning("Error disconnecting MCP client '%s': %s", name, exc)
        clients.clear()
        logger.info("MCP clients disconnected.")

    def resolve_permission(self, decision: str | bool, tool_name: str = "") -> None:
        if self._permission_future and not self._permission_future.done():
            if isinstance(decision, bool):
                decision = "once" if decision else "deny"
            self._permission_future.set_result((decision, tool_name))

    def resolve_skill_permission(self, granted: bool) -> None:
        if self._skill_permission_future and not self._skill_permission_future.done():
            self._skill_permission_future.set_result(granted)

    def resolve_mcp_trust(self, granted: bool) -> None:
        if self._mcp_trust_future and not self._mcp_trust_future.done():
            self._mcp_trust_future.set_result(granted)

    def _cancel_permission(self) -> None:
        if self._permission_future and not self._permission_future.done():
            self._permission_future.set_result(("deny", ""))
        if self._skill_permission_future and not self._skill_permission_future.done():
            self._skill_permission_future.set_result(False)
        if self._mcp_trust_future and not self._mcp_trust_future.done():
            self._mcp_trust_future.set_result(False)

    def record_skill_invocation(
        self,
        skill_name: str,
        skill_path: str,
        prompt: str,
        *,
        agent_scope: str = "main",
        turn_count: int = 0,
    ) -> None:
        if self._skill_persistence is None or not prompt:
            return
        self._skill_persistence.record_invocation(
            agent_scope,
            skill_name,
            skill_path,
            prompt,
            turn_count=turn_count,
        )

    def export_skill_recovery_snapshot(self) -> dict[str, object] | None:
        if self._skill_persistence is None:
            return None
        return self._skill_persistence.export_snapshot()

    def import_skill_recovery_snapshot(self, snapshot: dict[str, object] | None) -> None:
        if self._skill_persistence is None:
            return
        self._skill_persistence.import_snapshot(snapshot)

    def mark_skill_history_compacted(self, agent_scope: str = "main") -> None:
        """Enable recovery injection for scopes whose history has been compacted."""
        self._skill_recovery_ready[agent_scope] = True

    async def _init_mcp_tools(self) -> None:
        """Lazy MCP discovery - delegates ToolRegistry bridging to tools.mcp."""
        from ..tools.mcp.integration import register_mcp_tools

        await register_mcp_tools(self._registry, self.config.cwd, self._context)

    async def _maybe_prompt_mcp_trust(self) -> AsyncGenerator[StreamEvent, None]:
        pending = self._context.get("pending_mcp_trust") or []
        if not pending:
            return
        loop = asyncio.get_running_loop()
        self._mcp_trust_future = loop.create_future()
        try:
            yield StreamEvent(
                type="permission_needed",
                content="mcp_project_trust",
                metadata={
                    "mcp_trust_request": pending,
                    "dangerous": True,
                    "risk": "high",
                },
            )
            granted = await self._mcp_trust_future
        finally:
            self._mcp_trust_future = None

        if not granted:
            self._context["pending_mcp_trust"] = []
            return

        approvals = {
            item["name"]: item["fingerprint"]
            for item in pending
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("fingerprint"), str)
        }
        self._context["trusted_mcp_servers"] = approvals
        self._context["pending_mcp_trust"] = []
        await self._init_mcp_tools()

    async def _request_skill_shell_permission(self, request) -> bool:
        loop = asyncio.get_running_loop()
        self._skill_permission_future = loop.create_future()
        try:
            self._skill_permission_events.append(
                StreamEvent(
                    type="permission_needed",
                    content="",
                    metadata={
                        "skill_shell_request": request,
                        "dangerous": True,
                        "risk": "high",
                    },
                )
            )
            return await self._skill_permission_future
        finally:
            self._skill_permission_future = None

    def _inject_skill_runtime_attachments(self, state: AgentState) -> None:
        if self._skill_discovery is None:
            return

        current_cwd = resolve_skill_context_cwd(self.config.cwd, self._context)
        listing = self._skill_discovery.build_listing_message(
            self.config.api_max_tokens,
            cwd=current_cwd,
        )
        if listing is not None:
            _insert_before_current_user_message(state, listing)

        if (
            self._skill_persistence is not None
            and self._skill_recovery_ready.get("main", False)
        ):
            recovery = self._skill_persistence.build_recovery_message("main")
            if recovery is not None:
                _insert_before_current_user_message(state, recovery)

    @staticmethod
    def _strip_skill_messages(
        state: AgentState,
        sources: set[str],
    ) -> None:
        state.messages = strip_skill_context_messages(state.messages, sources=sources)

    async def _query_loop(self, state: AgentState) -> AsyncGenerator[StreamEvent, None]:
        output_recovery = OutputRecoveryState.from_config(self.config.api_max_tokens)
        ptl_recovery = PTLRecoveryManager(
            config=self.config,
            regions=self._l3_regions,
        )
        consecutive_api_errors = 0

        if self.config.mcp_enabled and not self._mcp_initialized:
            self._mcp_initialized = True
            try:
                await self._init_mcp_tools()
                async for event in self._maybe_prompt_mcp_trust():
                    yield event
            except Exception as exc:
                logger.warning("MCP initialization failed: %s", exc)

        from ..context import ContextPipeline
        from ..context.tokens import token_count_with_estimation

        state.messages = strip_memory_context_messages(
            state.messages,
            source=MEMORY_INDEX_SOURCE,
        )

        # Inject pending extraction results from the previous turn
        if self._extraction_controller.has_pending_result():
            result = self._extraction_controller.consume_result()
            if result and state.messages:
                _insert_before_current_user_message(
                    state,
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": result}],
                        },
                    )

        if self.config.auto_memory_enabled and self.config.auto_memory_directory:
            _inject_memory_index_context(state, self.config.auto_memory_directory)

        # Prefetch recalled memories briefly before the first request. If recall
        # is slow, let the model start and clean the task up on loop exit.
        recall_task: asyncio.Task | None = None
        if (
            self.config.auto_memory_enabled
            and self.config.auto_memory_directory
            and state.last_query
        ):
            recall_task = asyncio.create_task(self._run_memory_recall(state))
            recall_task.add_done_callback(_consume_task_exception)
            try:
                recalled = await asyncio.wait_for(
                    asyncio.shield(recall_task),
                    timeout=getattr(
                        self.config,
                        "memory_recall_prefetch_timeout_seconds",
                        0.25,
                    ),
                )
            except asyncio.TimeoutError:
                recalled = []
            if recalled:
                _inject_recalled_memories(state, recalled)

        while True:
            if self._aborted:
                self._last_state = state
                yield StreamEvent(type="done", content="Aborted by user.")
                break

            max_parent_turns = getattr(self.config, "max_parent_turns", MAX_PARENT_TURNS)
            if state.turn_count >= max_parent_turns:
                logger.warning("Parent loop hit turn limit (%d) - stopping.", max_parent_turns)
                self._last_state = state
                yield StreamEvent(
                    type="error",
                    content=(
                        f"Reached maximum turns ({max_parent_turns}). "
                        "The task may be too complex or stuck in a loop. "
                        "Try breaking it into smaller steps."
                    ),
                )
                yield StreamEvent(type="done", content="")
                await _cancel_recall_prefetch(recall_task)
                return

            self._strip_skill_messages(
                state,
                {SKILL_LISTING_SOURCE, SKILL_RECOVERY_SOURCE},
            )

            pipeline = ContextPipeline(self.config)
            current_total_tokens = token_count_with_estimation(state.messages)
            if current_total_tokens > int(200_000 * self.config.context_compress_threshold):
                state.messages, _stats = await pipeline.compress(
                    state.messages,
                    current_tokens=current_total_tokens,
                    system_prompt=state.system_prompt,
                    state=state,
                )
                if _stats.level_reached >= 1:
                    self.mark_skill_history_compacted("main")

            self._inject_skill_runtime_attachments(state)
            await self._inject_pending_task_notifications(state)
            inline_runtime = collect_inline_skill_runtime(state.messages)
            active_registry = self._registry
            if inline_runtime.allowed_tool_names is not None:
                active_registry = self._registry.filtered_copy(
                    allow_list=set(inline_runtime.allowed_tool_names)
                )
            current_cwd = resolve_skill_context_cwd(self.config.cwd, self._context)
            extra_context = {
                key: value
                for key, value in self._context.items()
                if key != "cwd"
            }

            turn = ModelTurn()
            pending_skill_messages: list[dict[str, Any]] = []

            executor = StreamingToolExecutor(
                registry=active_registry,
                config=self.config,
                state=state,
                context={
                    "cwd": str(current_cwd),
                    "config": self.config,
                    "allowed_read_roots": [str(self.config.cwd)],
                    "_registry": active_registry,
                    "parent_state": state,
                    "_pending_skill_messages": pending_skill_messages,
                    "_request_skill_shell_permission": self._request_skill_shell_permission,
                    "_skill_persistence": self._skill_persistence,
                    "_skill_agent_scope": "main",
                    **extra_context,
                },
            )

            try:
                thinking_budget = self._resolve_effort_to_thinking_budget(inline_runtime.effort)
                client = self._create_main_loop_client(
                    max_tokens=output_recovery.current_max_tokens,
                    model=inline_runtime.model_override,
                    thinking_budget_tokens=thinking_budget,
                )
                all_messages = self._build_messages(state)
                tool_schemas = active_registry.get_api_schemas()

                async for event in client.stream_chat(
                    system_prompt=state.system_prompt,
                    messages=all_messages,
                    tools=tool_schemas,
                ):
                    action = self._handle_stream_event(
                        event,
                        turn,
                        executor,
                        state,
                        consecutive_api_errors,
                    )
                    consecutive_api_errors = action.consecutive_api_errors
                    if action.event is not None:
                        yield action.event
                    if action.action == _STREAM_ACTION_RETURN:
                        if consecutive_api_errors >= MAX_API_ERROR_RETRIES:
                            yield self._api_error_event(event.get("message", "Unknown API error"))
                        yield StreamEvent(type="done", content="")
                        await _cancel_recall_prefetch(recall_task)
                        return
                    if action.action == _STREAM_ACTION_BREAK:
                        break

            except Exception as exc:
                error_str = str(exc)
                if _is_fatal_api_error(error_str):
                    self._remember_api_error(state, error_str)
                    self._last_state = state
                    yield StreamEvent(type="error", content=error_str)
                    yield StreamEvent(type="done", content="")
                    await _cancel_recall_prefetch(recall_task)
                    return
                if is_ptl_error_message(error_str):
                    turn.ptl_detected = True
                    turn.ptl_error_msg = error_str
                else:
                    consecutive_api_errors += 1
                    logger.exception("API call failed - retry %d/%d", consecutive_api_errors, MAX_API_ERROR_RETRIES)
                    self._remember_api_error(state, error_str)
                    if consecutive_api_errors >= MAX_API_ERROR_RETRIES:
                        yield self._api_error_event(error_str)
                        self._last_state = state
                        yield StreamEvent(type="done", content="")
                        await _cancel_recall_prefetch(recall_task)
                        return
                    yield StreamEvent(type="error", content=f"API error (recovered): {error_str[:300]}")
                    continue

            if not turn.stream_had_error:
                consecutive_api_errors = 0

            # Non-fatal, non-PTL stream error with retry budget remaining —
            # retry the request rather than exiting the loop.
            if (
                turn.stream_had_error
                and not turn.tool_calls
                and consecutive_api_errors < MAX_API_ERROR_RETRIES
            ):
                continue

            if turn.ptl_detected:
                action, recovery_event = await ptl_recovery.recover(state, turn.ptl_error_msg)
                self._l3_regions = ptl_recovery.regions
                if action == "retry":
                    output_recovery.reset_after_history_replace(self.config.api_max_tokens)
                    continue
                if recovery_event is not None:
                    yield recovery_event
                self._last_state = state
                yield StreamEvent(type="done", content="")
                await _cancel_recall_prefetch(recall_task)
                return

            if turn.output_truncated:
                recovery_result = handle_output_truncation(
                    state=state,
                    recovery=output_recovery,
                    tool_calls=turn.tool_calls,
                    thinking_content=turn.thinking_content,
                    full_text=turn.full_text,
                    current_message_id=turn.current_message_id,
                    input_tokens=turn.input_tokens,
                    output_tokens=turn.output_tokens,
                )
                if recovery_result.should_continue:
                    continue
                if recovery_result.should_return:
                    if recovery_result.event is not None:
                        yield recovery_result.event
                    self._last_state = state
                    yield StreamEvent(type="done", content="")
                    await _cancel_recall_prefetch(recall_task)
                    return

            output_recovery.reset_retries()
            commit_assistant_turn(
                state,
                thinking_content=turn.thinking_content,
                full_text=turn.full_text,
                tool_calls=turn.tool_calls,
                message_id=turn.current_message_id,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
            )

            if not turn.tool_calls:
                # ── Background memory extraction trigger ─────────
                if (
                    self.config.auto_memory_enabled
                    and self.config.auto_memory_directory
                    and state.last_query
                ):
                    from pathlib import Path
                    memory_dir = Path(self.config.auto_memory_directory)
                    if memory_dir.exists():
                        self._extraction_controller.schedule(state, memory_dir)

                cost = self._calculate_cost(state)
                self._last_state = state
                yield StreamEvent(type="cost", content=f"${cost:.4f}", metadata={"cost": cost})
                yield StreamEvent(type="done", content="")
                break

            async for permission_event in self._resolve_tool_permissions(
                turn.tool_calls,
                executor,
                state,
            ):
                yield permission_event

            async for event in self._execute_and_commit_tools(
                state,
                executor,
                pending_skill_messages,
            ):
                yield event

            state.turn_count += 1
            for tc in turn.tool_calls:
                if state.permission_state.is_tool_confirmed(tc.name):
                    file_path = tc.input.get("file_path")
                    if file_path:
                        state.permission_state.confirm_path(file_path)

        await _cancel_recall_prefetch(recall_task)

    def _build_client(
        self,
        max_tokens: int | None = None,
        model: str | None = None,
        thinking_budget_tokens: int | None = None,
    ) -> APIClient:
        from ..api.client import create_llm_client

        return create_llm_client(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=model or self.config.api_model,
            max_tokens=max_tokens if max_tokens is not None else self.config.api_max_tokens,
            thinking_budget_tokens=thinking_budget_tokens,
            retry_config=RetryConfig(),
        )

    def _create_main_loop_client(
        self,
        *,
        max_tokens: int | None = None,
        model: str | None = None,
        thinking_budget_tokens: int | None = None,
    ) -> APIClient:
        """Create a model client while remaining compatible with test seams.

        Tests in this repo frequently monkeypatch ``_build_client`` with a small
        lambda that only accepts ``max_tokens``. Filter kwargs against the
        factory signature so inline-skill runtime overrides do not break the
        rest of the loop.
        """
        factory = self._build_client
        kwargs = {
            "max_tokens": max_tokens,
            "model": model,
            "thinking_budget_tokens": thinking_budget_tokens,
        }
        filtered_kwargs = self._filter_client_factory_kwargs(factory, kwargs)
        return factory(**filtered_kwargs)

    @staticmethod
    def _filter_client_factory_kwargs(
        factory: Any,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return {
                key: value
                for key, value in kwargs.items()
                if value is not None
            }

        parameters = signature.parameters.values()
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
            return {
                key: value
                for key, value in kwargs.items()
                if value is not None
            }

        allowed = set(signature.parameters)
        return {
            key: value
            for key, value in kwargs.items()
            if key in allowed
        }

    @staticmethod
    def _resolve_effort_to_thinking_budget(effort: str | int | None) -> int | None:
        """Map an inline skill effort level to a thinking budget token count."""
        if effort is None:
            return None
        if isinstance(effort, int):
            return max(0, effort) or None
        return EFFORT_THINKING_BUDGETS.get(effort)

    def _build_messages(self, state: AgentState) -> list[dict[str, Any]]:
        from ..context.collapse import project_collapsed_view
        from .normalize import normalize_messages

        if self._l3_regions:
            projected = project_collapsed_view(list(state.messages), self._l3_regions)
        else:
            projected = list(state.messages)

        repaired = _repair_orphan_tools(projected)
        normalized = normalize_messages(
            repaired,
            model_family=self.config.api_model,
            recent_errors=state.recent_api_errors,
        )
        api_ready = _strip_message_metadata(normalized)
        return self._apply_rolling_cache(api_ready, state)

    def _apply_rolling_cache(
        self,
        messages: list[dict[str, Any]],
        state: AgentState,
    ) -> list[dict[str, Any]]:
        # cache_control is an Anthropic extension — skip for non-Anthropic models
        if not self.config.api_model.lower().startswith("claude-"):
            state.cache_breakpoints.clear()
            return messages

        n = len(messages)
        if n < 8:
            state.cache_breakpoints.clear()
            return messages

        boundary = n - 4
        breakpoints = {b for b in state.cache_breakpoints if 0 <= b < boundary}
        _MAX_HISTORY_BREAKPOINTS = 2
        if len(breakpoints) < _MAX_HISTORY_BREAKPOINTS:
            candidates = []
            if n >= 12:
                candidates.append(max(n // 3, 2))
            if n >= 18:
                candidates.append(max(2 * n // 3, (n // 3) + 5))

            for candidate in candidates:
                if len(breakpoints) >= _MAX_HISTORY_BREAKPOINTS:
                    break
                if candidate >= boundary:
                    continue
                if any(abs(candidate - b) < 4 for b in breakpoints):
                    continue
                breakpoints.add(candidate)

        if not breakpoints:
            state.cache_breakpoints.clear()
            return messages

        state.cache_breakpoints = breakpoints
        result: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i not in breakpoints:
                result.append(msg)
                continue

            content = msg.get("content", [])
            if isinstance(content, list) and content:
                new_content = list(content)
                new_content[-1] = {
                    **new_content[-1],
                    "cache_control": {"type": "ephemeral"},
                }
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result

    def _calculate_cost(self, state: AgentState) -> float:
        from ..api.client import get_pricing

        pricing = get_pricing(self.config.api_model)
        input_price = (
            self.config.api_input_price_per_1k
            if self.config.api_input_price_per_1k is not None
            else pricing["input"] / 1000
        )
        output_price = (
            self.config.api_output_price_per_1k
            if self.config.api_output_price_per_1k is not None
            else pricing["output"] / 1000
        )
        input_cost = (state.total_input_tokens / 1000) * input_price
        output_cost = (state.total_output_tokens / 1000) * output_price
        return input_cost + output_cost

    async def _inject_pending_task_notifications(self, state: AgentState) -> None:
        drained = await self.task_runtime.drain_pending_notifications(
            scope_id=str(self._context.get("scope_id", "main") or "main"),
            current_task_id=self._context.get("current_task_id"),
        )
        for message in drained:
            _insert_before_current_user_message(state, message)

    def _handle_stream_event(
        self,
        event: dict[str, Any],
        turn: ModelTurn,
        executor: StreamingToolExecutor,
        state: AgentState,
        consecutive_api_errors: int,
    ) -> StreamAction:
        event_type = event["type"]

        if event_type == "text_delta":
            turn.full_text += event["text"]
            return StreamAction(
                event=StreamEvent(type="text", content=event["text"]),
                consecutive_api_errors=consecutive_api_errors,
            )

        if event_type == "message_id":
            turn.current_message_id = event["id"]
            return StreamAction(consecutive_api_errors=consecutive_api_errors)

        if event_type == "thinking":
            turn.thinking_content.append(
                {
                    "type": "thinking",
                    "thinking": event["text"],
                    "signature": event.get("signature", ""),
                }
            )
            return StreamAction(
                event=StreamEvent(
                    type="thinking",
                    content=event["text"],
                    metadata={"signature": event.get("signature", "")},
                ),
                consecutive_api_errors=consecutive_api_errors,
            )

        if event_type == "redacted_thinking":
            turn.thinking_content.append({
                "type": "redacted_thinking",
                "data": event.get("data", ""),
            })
            return StreamAction(
                event=StreamEvent(type="thinking", content="[redacted thinking]"),
                consecutive_api_errors=consecutive_api_errors,
            )

        if event_type == "thinking_delta":
            if turn.thinking_content and turn.thinking_content[-1].get("type") == "thinking":
                turn.thinking_content[-1]["thinking"] += event.get("text", "")
            return StreamAction(
                event=StreamEvent(type="thinking", content=event.get("text", "")),
                consecutive_api_errors=consecutive_api_errors,
            )

        if event_type == "signature_delta":
            if turn.thinking_content and turn.thinking_content[-1].get("type") == "thinking":
                existing = turn.thinking_content[-1].get("signature", "")
                turn.thinking_content[-1]["signature"] = existing + event.get("signature", "")
            return StreamAction(consecutive_api_errors=consecutive_api_errors)

        if event_type == "tool_use":
            tc = ToolCall(
                id=event["id"],
                name=event["name"],
                input=event["input"],
            )
            turn.tool_calls.append(tc)
            executor.add_tool(tc)
            return StreamAction(
                event=StreamEvent(
                    type="tool_call",
                    content=event["name"],
                    metadata={"id": event["id"], "input": event["input"]},
                ),
                consecutive_api_errors=consecutive_api_errors,
            )

        if event_type == "usage":
            turn.input_tokens = event.get("input_tokens", 0)
            turn.output_tokens = event.get("output_tokens", 0)
            return StreamAction(consecutive_api_errors=consecutive_api_errors)

        if event_type == "stop_reason":
            reason = event.get("stop_reason", event.get("reason", ""))
            if is_output_truncated_stop_reason(reason):
                turn.output_truncated = True
            return StreamAction(consecutive_api_errors=consecutive_api_errors)

        if event_type == "error":
            return self._handle_stream_error_event(
                event,
                turn,
                state,
                consecutive_api_errors,
            )

        return StreamAction(consecutive_api_errors=consecutive_api_errors)

    def _handle_stream_error_event(
        self,
        event: dict[str, Any],
        turn: ModelTurn,
        state: AgentState,
        consecutive_api_errors: int,
    ) -> StreamAction:
        msg = event.get("message", "Unknown API error")
        if is_ptl_error_message(msg) and not _is_fatal_api_error(msg):
            turn.ptl_detected = True
            turn.ptl_error_msg = msg
            return StreamAction(
                action=_STREAM_ACTION_BREAK,
                consecutive_api_errors=consecutive_api_errors,
            )

        turn.stream_had_error = True
        consecutive_api_errors += 1
        self._remember_api_error(state, msg)

        if consecutive_api_errors >= MAX_API_ERROR_RETRIES:
            self._last_state = state
            return StreamAction(
                action=_STREAM_ACTION_RETURN,
                event=StreamEvent(type="error", content=msg),
                consecutive_api_errors=consecutive_api_errors,
            )

        return StreamAction(
            action=_STREAM_ACTION_BREAK,
            event=StreamEvent(type="error", content=msg),
            consecutive_api_errors=consecutive_api_errors,
        )

    def _remember_api_error(self, state: AgentState, msg: str) -> None:
        state.recent_api_errors.append(msg)
        if len(state.recent_api_errors) > 5:
            state.recent_api_errors = state.recent_api_errors[-5:]

    def _api_error_event(self, msg: str) -> StreamEvent:
        return StreamEvent(
            type="error",
            content=(
                f"API error after {MAX_API_ERROR_RETRIES} retries: "
                f"{msg[:300]}. The request may be malformed. "
                "Try /clear to start a fresh session."
            ),
        )

    async def _execute_and_commit_tools(
        self,
        state: AgentState,
        executor: StreamingToolExecutor,
        pending_skill_messages: list[dict[str, Any]],
    ) -> AsyncGenerator[StreamEvent, None]:
        tool_results: list[dict[str, Any]] = []
        while executor.has_pending_work():
            for event in self._drain_progress_events(executor):
                yield event
            while self._skill_permission_events:
                yield self._skill_permission_events.pop(0)
            tool_results.extend(executor.get_completed_results())
            if executor.has_pending_work():
                await executor.wait_for_activity()

        tool_results.extend(executor.get_completed_results())
        tool_results.extend(await executor.get_remaining_results())

        for event in self._drain_progress_events(executor):
            yield event
        while self._skill_permission_events:
            yield self._skill_permission_events.pop(0)

        async for event in self._emit_tool_result_events(state, executor, tool_results):
            yield event

        tool_observations = self._collect_tool_observations(executor, tool_results)
        commit_tool_results_turn(state, tool_results, executor)
        if pending_skill_messages:
            state.messages.extend(pending_skill_messages)
            pending_skill_messages.clear()
        await self._append_fresh_recalled_memories(state, tool_observations)

    def _drain_progress_events(
        self,
        executor: StreamingToolExecutor,
    ) -> list[StreamEvent]:
        return [build_progress_event(pe) for pe in executor.drain_progress()]

    async def _emit_tool_result_events(
        self,
        state: AgentState,
        executor: StreamingToolExecutor,
        tool_results: list[dict[str, Any]],
    ) -> AsyncGenerator[StreamEvent, None]:
        for tr in tool_results:
            tid = tr.get("tool_use_id", "")
            slot = executor.get_slot(tid)
            if slot is None:
                continue

            if slot.is_error:
                await self.post_tool_fail(slot.tc, tr)
                tool = executor._registry.get(slot.tc.name)
                if tool is not None and tool.supports_sibling_abort():
                    executor.abort_siblings(tid)
            else:
                await self.post_tool_use(slot.tc, tr, state)
                state.denied_tool_calls.pop(slot.tc.name, None)
                state.tool_errors.pop(slot.tc.name, None)

            yield StreamEvent(
                type="tool_result",
                content="",
                metadata={
                    "tool_use_id": tid,
                    "tool_name": slot.tc.name,
                    "result": slot.truncated[:500] if slot.truncated else "",
                    "is_error": slot.is_error,
                },
            )

    async def _resolve_tool_permissions(
        self,
        tool_calls: list[ToolCall],
        executor: StreamingToolExecutor,
        state: AgentState,
    ) -> AsyncGenerator[StreamEvent, None]:
        resolver = PermissionResolver(
            registry=executor._registry,
            check_permission=self._check_permission_chain,
            pre_tool_hook=self.pre_tool_hook,
            create_permission_future=lambda: asyncio.get_running_loop().create_future(),
            set_permission_future=lambda future: setattr(self, "_permission_future", future),
        )
        async for event in resolver.resolve(
            tool_calls=tool_calls,
            executor=executor,
            state=state,
            is_aborted=lambda: self._aborted,
        ):
            yield event

    async def pre_tool_hook(self, tc: ToolCall) -> None:
        """Stage 3: override for logging, auditing, etc."""

    async def post_tool_use(
        self, tc: ToolCall, result: dict[str, Any], state: AgentState | None = None,
    ) -> None:
        """Stage 7 (success): detect memory writes for extraction mutual exclusion."""
        if (
            self.config.auto_memory_enabled
            and self.config.auto_memory_directory
            and tc.name in ("write_file", "edit_file")
        ):
            file_path = tc.input.get("file_path", "")
            if file_path:
                from pathlib import Path
                try:
                    resolved = Path(file_path).resolve()
                    mem_root = Path(self.config.auto_memory_directory).resolve()
                    if resolved == mem_root or mem_root in resolved.parents:
                        target = state if state is not None else self._last_state
                        if target is not None:
                            target.memory_writes_since_extraction = True
                        try:
                            from ..memory.index import write_memory_index

                            write_memory_index(mem_root)
                        except Exception:
                            logger.debug("Failed to refresh MEMORY.md", exc_info=True)
                except Exception:
                    logger.debug("Failed to detect memory writes for path resolution", exc_info=True)

    async def post_tool_fail(self, tc: ToolCall, result: dict[str, Any]) -> None:
        """Stage 7 (failure): override for error telemetry, alerting, etc."""

    def _check_permission_chain(self, tc: ToolCall, tool: Any, state: AgentState) -> bool:
        if tool is None:
            return True
        if state.permission_state.yolo_mode:
            return False
        try:
            validated_input = tool.input_schema.model_validate(tc.input)
        except Exception:
            return True
        if tool.is_read_only(validated_input):
            return False
        if tc.name == "run_shell":
            command = tc.input.get("command", "")
            if command and state.permission_state.is_command_rule_confirmed(command):
                return False
        if state.permission_state.is_tool_confirmed(tc.name):
            return False

        # L4: Polymorphic file-path confirmation (Insight 4.11.1).
        # Tools opt in via confirms_file_paths() instead of a name check.
        if tool.confirms_file_paths():
            file_path = getattr(validated_input, "file_path", None)
            if file_path and state.permission_state.is_path_confirmed(file_path):
                return False

        return tool.needs_permission(validated_input)

    # ── Memory recall ────────────────────────────────────────────

    async def _run_memory_recall(self, state: AgentState) -> list[MemoryRecall]:
        """Run the full memory recall pipeline in the background.

        Returns up to 5 relevant MemoryRecall objects, or an empty list
        on any error (graceful degradation — memory is best-effort).
        """
        return await self._run_memory_recall_with_query(state, query=state.last_query)

    async def _run_memory_recall_with_query(
        self,
        state: AgentState,
        *,
        query: str,
        recent_tools: list[str] | None = None,
        already_surfaced: set[str] | None = None,
    ) -> list[MemoryRecall]:
        """Run memory recall for a specific query within the current session."""
        try:
            from pathlib import Path

            memory_dir = Path(self.config.auto_memory_directory)
            if not memory_dir.exists() or not query:
                return []

            async def _client_factory():
                return self._build_client(max_tokens=256)

            if recent_tools is None:
                recent_tools = self._get_recent_tool_names(state)
            if already_surfaced is None:
                already_surfaced = recalled_memory_ids(state.messages)

            main_results, agent_results = await asyncio.gather(
                recall_memories_for_query(
                    query=query,
                    memory_dir=memory_dir,
                    client_factory=_client_factory,
                    recent_tools=recent_tools,
                    already_surfaced=already_surfaced,
                ),
                recall_agent_memories_for_query(
                    agent_type="main",
                    project_root=self.config.cwd,
                    query=query,
                    client_factory=_client_factory,
                    recent_tools=recent_tools,
                    already_surfaced=already_surfaced,
                ),
            )
            return list(main_results) + list(agent_results)
        except Exception:
            logger.debug("Memory recall pipeline failed", exc_info=True)
            return []

    @staticmethod
    def _get_recent_tool_names(state: AgentState) -> list[str]:
        """Extract recently used tool names from the last few assistant turns."""
        names: list[str] = []
        for msg in reversed(state.messages[-10:]):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name and name not in names:
                            names.append(name)
        return names

    def _collect_tool_observations(
        self,
        executor: StreamingToolExecutor,
        tool_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Capture the latest tool outcomes for same-turn follow-up recall."""
        observations: list[dict[str, Any]] = []
        for result in tool_results:
            if result.get("type") != "tool_result":
                continue
            tid = result.get("tool_use_id", "")
            if not tid:
                continue
            slot = executor.get_slot(tid)
            if slot is None:
                continue
            observations.append(
                {
                    "call": slot.tc,
                    "tool": executor._registry.get(slot.tc.name),
                    "is_error": slot.is_error,
                    "content": slot.truncated if slot.truncated else result.get("content", ""),
                }
            )
        return observations

    async def _append_fresh_recalled_memories(
        self,
        state: AgentState,
        tool_observations: list[dict[str, Any]],
    ) -> None:
        """Append newly recalled memories after tool results in the same user turn."""
        if not self.config.auto_memory_enabled or not self.config.auto_memory_directory:
            return
        if not state.last_query:
            return
        if not self._should_trigger_followup_recall(tool_observations):
            return

        recall_query = self._build_followup_recall_query(
            state.last_query,
            tool_observations,
        )
        if not recall_query:
            return

        recalled = await self._run_memory_recall_with_query(
            state,
            query=recall_query,
            recent_tools=self._get_recent_tool_names(state),
            already_surfaced=recalled_memory_ids(state.messages),
        )
        if not recalled:
            return

        message = build_recalled_memories_message(recalled)
        if message is not None:
            state.messages.append(message)

    @staticmethod
    def _should_trigger_followup_recall(tool_observations: list[dict[str, Any]]) -> bool:
        """Trigger follow-up recall after read observations or tool failures."""
        if not tool_observations:
            return False

        for observation in tool_observations:
            if observation.get("is_error"):
                return True

        return any(
            CoreExecutionEngine._is_read_like_tool(
                observation["call"].name,
                observation.get("tool"),
                observation["call"].input,
            )
            for observation in tool_observations
        )

    @staticmethod
    def _is_read_like_tool(name: str, tool: Any, raw_input: dict[str, Any]) -> bool:
        has_location_hint = any(
            isinstance(raw_input.get(key), str) and raw_input.get(key, "").strip()
            for key in ("file_path", "path", "pattern", "query")
        )
        if not has_location_hint:
            return False

        if name in _READ_LIKE_TOOL_NAMES:
            return True

        if tool is None:
            return False

        try:
            validated_input = tool.input_schema.model_validate(raw_input)
        except Exception:
            validated_input = None

        try:
            return bool(tool.is_read_only(validated_input))
        except TypeError:
            return bool(tool.is_read_only())
        except Exception:
            return False

    def _build_followup_recall_query(
        self,
        task_query: str,
        tool_observations: list[dict[str, Any]],
    ) -> str:
        """Build a compact recall query from the task and latest tool outcomes."""
        errors: list[str] = []
        observations: list[str] = []

        for observation in tool_observations:
            call = observation["call"]
            details = self._format_tool_input_for_recall(call.input)
            label = call.name if not details else f"{call.name} ({details})"
            content = self._clip_recall_text(observation.get("content", ""))
            line = f"- {label}: {content}"
            if observation.get("is_error"):
                errors.append(line)
            elif self._is_read_like_tool(call.name, observation.get("tool"), call.input):
                observations.append(line)

        parts = [f"Task: {task_query}"]
        if errors:
            parts.extend(["", "Recent tool errors:", *errors[:3]])
        elif observations:
            parts.extend(["", "Recent observations:", *observations[:3]])

        return "\n".join(parts)

    @staticmethod
    def _format_tool_input_for_recall(raw_input: dict[str, Any]) -> str:
        """Extract compact location hints from a tool call input."""
        if not isinstance(raw_input, dict):
            return ""

        hints: list[str] = []
        for key in ("file_path", "path", "pattern", "query", "command"):
            value = raw_input.get(key)
            if isinstance(value, str) and value.strip():
                hints.append(f"{key}={value.strip()}")
            if len(hints) == 2:
                break
        return ", ".join(hints)

    @staticmethod
    def _clip_recall_text(text: str, *, limit: int = 400) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(limit - 3, 1)] + "..."


def _inject_recalled_memories(state: AgentState, recalled: list[MemoryRecall]) -> None:
    """Inject recalled memory content as a system-reminder user message.

    Inserted before the current user turn so the model can use them while
    answering that same turn, without displacing the user's message as the
    most recent instruction.
    """
    message = build_recalled_memories_message(recalled)
    if message is None:
        return

    _insert_before_current_user_message(state, message)


def _inject_memory_index_context(state: AgentState, memory_dir: str) -> None:
    """Inject the current MEMORY.md index as hidden user context."""
    from pathlib import Path

    message = build_memory_index_message(Path(memory_dir))
    if message is not None:
        _insert_before_current_user_message(state, message)


def _strip_message_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal message-only metadata before sending requests to the API."""
    api_messages: list[dict[str, Any]] = []
    for msg in messages:
        cleaned = {
            key: value
            for key, value in msg.items()
            if key not in {"metadata", "isMeta"}
        }
        api_messages.append(cleaned)
    return api_messages


def _insert_before_current_user_message(
    state: AgentState,
    message: dict[str, Any],
) -> None:
    """Insert metadata before the current user query when possible."""
    if state.messages and state.messages[-1].get("role") == "user":
        state.messages.insert(len(state.messages) - 1, message)
    else:
        state.messages.append(message)


def create_core_engine(
    config: Config | None = None,
    *,
    skill_registry: SkillRegistry | None = None,
    skill_executor: SkillExecutor | None = None,
) -> CoreExecutionEngine:
    return CoreExecutionEngine(
        config=config,
        skill_registry=skill_registry,
        skill_executor=skill_executor,
    )
