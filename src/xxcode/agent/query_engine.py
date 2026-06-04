"""QueryEngine - outer session manager."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import uuid
from collections.abc import AsyncGenerator

from ..config import Config, get_config
from ..context.builder import build_memory_section, build_system_prompt
from ..skills import (
    PromptProcessor,
    resolve_skill_context_cwd,
    SkillExecutor,
    SkillLoader,
    SkillRegistry,
    strip_skill_context_messages,
)
from .messages import add_usage
from .messages import append_assistant_message
from .events import StreamEvent
from .loop import CoreExecutionEngine
from .state import AgentState

logger = logging.getLogger(__name__)


class QueryEngine:
    """Outer session manager: state init, slash handling, and loop delegation."""

    def __init__(self, config: Config | None = None):
        self.config = config or get_config()
        self.skill_loader = SkillLoader(self.config)
        self.skill_registry = SkillRegistry(root=self.config.cwd)
        if self.config.skills_enabled:
            for skill in self.skill_loader.load_frontmatter_only():
                self.skill_registry.register(skill)
        self.skill_executor = SkillExecutor(
            self.skill_loader,
            PromptProcessor(self.config),
        )
        self.core_engine = CoreExecutionEngine(
            config=self.config,
            skill_registry=self.skill_registry if self.config.skills_enabled else None,
            skill_executor=self.skill_executor if self.config.skills_enabled else None,
        )
        self._session_cost: float = 0.0
        self._skill_permission_future: asyncio.Future | None = None
        self._skill_permission_queue: asyncio.Queue[StreamEvent] | None = None

    @property
    def _last_state(self) -> AgentState | None:
        return self.core_engine._last_state

    @_last_state.setter
    def _last_state(self, value: AgentState | None) -> None:
        self.core_engine._last_state = value

    def abort(self) -> None:
        self.core_engine.abort()

    def reset(self) -> None:
        self.core_engine.reset()
        self._session_cost = 0.0

    def resolve_permission(self, decision: str | bool, tool_name: str = "") -> None:
        self.core_engine.resolve_permission(decision, tool_name)

    def resolve_skill_permission(self, granted: bool) -> None:
        if self._skill_permission_future and not self._skill_permission_future.done():
            self._skill_permission_future.set_result(granted)
            return
        self.core_engine.resolve_skill_permission(granted)

    def resolve_mcp_trust(self, granted: bool) -> None:
        self.core_engine.resolve_mcp_trust(granted)

    async def shutdown(self) -> None:
        await self.core_engine.shutdown()
        self._session_cost = 0.0

    async def clear_mcp(self) -> None:
        await self.core_engine.clear_mcp()

    async def _compact(self, state: AgentState) -> AgentState:
        from ..context import ContextPipeline
        from ..context.tokens import token_count_with_estimation

        pipeline = ContextPipeline(self.config)
        state.messages = strip_skill_context_messages(state.messages)
        current_tokens = token_count_with_estimation(state.messages)
        compressed, stats = await pipeline.compress(
            state.messages,
            current_tokens=current_tokens,
            system_prompt=state.system_prompt,
            state=state,
        )

        if stats.level_reached >= 1:
            state.messages = compressed
            state.cache_breakpoints.clear()
            self.core_engine.mark_skill_history_compacted("main")
            logger.info(
                "Manual compact: %d -> %d tokens (level %d)",
                stats.tokens_before, stats.tokens_after, stats.level_reached,
            )
        else:
            logger.info("Manual compact: no compression needed")

        return state

    async def submit_message(
        self,
        user_prompt: str,
        state: AgentState | None = None,
        *,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        if user_prompt.startswith("/"):
            cmd = user_prompt[1:].strip().lower()
            if cmd in ("help", "cost", "tokens", "sessions", "compact", "compress"):
                yield StreamEvent(
                    type="text",
                    content=f"[system] The '{user_prompt}' command is handled by the REPL.\n",
                )
                yield StreamEvent(type="done", content="")
                return

        skill_session_id = session_id or uuid.uuid4().hex[:12]

        state = self._initialize_state(state)

        self._strip_transient_skill_messages(state)
        current_cwd = resolve_skill_context_cwd(
            self.config.cwd,
            self.core_engine._context,
        )

        self._build_or_refresh_system_prompt(state, current_cwd)

        skill_call = self._parse_skill_invocation(user_prompt)
        if user_prompt.startswith("/") and skill_call is None:
            yield StreamEvent(
                type="error",
                content=f"Unknown command: {user_prompt}",
            )
            yield StreamEvent(type="done", content="")
            return

        normalized_prompt = user_prompt
        if skill_call is not None:
            skill, skill_args = skill_call
            skill_context = {
                **self.core_engine._context,
                "_session_id": skill_session_id,
            }
            try:
                self._skill_permission_queue = asyncio.Queue()
                render_task = asyncio.create_task(
                    self.skill_executor.execute(
                        skill,
                        skill_args,
                        session_id=skill_session_id,
                        approve_project_shell=self._approve_project_skill_shell,
                        base_registry=self.core_engine._registry,
                        parent_state=state,
                        extra_context=skill_context,
                    )
                )
                try:
                    while True:
                        queue_task = asyncio.create_task(self._skill_permission_queue.get())
                        done, _pending = await asyncio.wait(
                            {render_task, queue_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if render_task in done:
                            queue_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await queue_task
                            execution = await render_task
                            break
                        permission_event = queue_task.result()
                        if permission_event is not None:
                            yield permission_event
                finally:
                    self._skill_permission_queue = None
            except PermissionError as exc:
                yield StreamEvent(type="error", content=str(exc))
                yield StreamEvent(type="done", content="")
                return
            except Exception as exc:
                logger.exception("Skill execution failed")
                yield StreamEvent(type="error", content=f"Skill execution failed: {exc}")
                yield StreamEvent(type="done", content="")
                return

            if execution.mode != "fork":
                state.messages.append(
                    self.skill_executor.build_inline_skill_message(
                        skill,
                        execution.prompt or "",
                    )
                )
            self.core_engine.record_skill_invocation(
                skill.canonical_name,
                str(skill.skill_file or skill.directory or ""),
                execution.prompt or "",
                turn_count=state.turn_count,
            )
            normalized_prompt = self._normalize_skill_user_prompt(
                skill.canonical_name,
                skill_args,
            )

            if execution.mode == "fork":
                result_text = execution.result_text or (
                    f"Skill '{skill.canonical_name}' completed."
                )
                add_usage(
                    state,
                    execution.input_tokens,
                    execution.output_tokens,
                )
                self._commit_user_turn(state, normalized_prompt)
                append_assistant_message(
                    state,
                    thinking_content=[],
                    full_text=result_text,
                    tool_calls=[],
                    message_id=None,
                )
                state.turn_count += 1
                cost = self.core_engine._calculate_cost(state)
                self._session_cost = cost
                self._last_state = state
                yield StreamEvent(type="text", content=result_text)
                yield StreamEvent(
                    type="cost",
                    content=f"${cost:.4f}",
                    metadata={"cost": cost},
                )
                yield StreamEvent(type="done", content="")
                return

        self._commit_user_turn(state, normalized_prompt)

        async for event in self.core_engine._query_loop(state):
            yield event

            if event.type == "cost":
                cost = event.metadata.get("cost", 0) if event.metadata else 0
                self._session_cost = cost
                if cost > self.config.max_budget_usd:
                    logger.warning(
                        "Budget exceeded: $%.4f > $%.2f - stopping session.",
                        cost, self.config.max_budget_usd,
                    )
                    yield StreamEvent(
                        type="error",
                        content=(
                            f"Session budget exceeded: ${cost:.4f} > "
                            f"${self.config.max_budget_usd:.2f}. "
                            f"Start a new session to continue."
                        ),
                    )
                    yield StreamEvent(type="done", content="")
                    break

    def _initialize_state(self, state: AgentState | None) -> AgentState:
        if state is None:
            state = AgentState()
            if self.config.yolo:
                state.permission_state.yolo_mode = True
        return state

    def _build_or_refresh_system_prompt(self, state: AgentState, current_cwd) -> None:
        if self.config.auto_memory_enabled and self.config.auto_memory_directory:
            state.system_prompt = build_system_prompt(
                current_cwd,
                memory_section=build_memory_section(self.config),
            )
        elif not state.system_prompt:
            state.system_prompt = build_system_prompt(current_cwd)

    @staticmethod
    def _commit_user_turn(state: AgentState, normalized_prompt: str) -> None:
        state.last_query = normalized_prompt
        state.user_turn_count += 1
        state.memory_writes_since_extraction = False
        state.messages.append({
            "role": "user",
            "content": [{"type": "text", "text": normalized_prompt}],
        })

    def _parse_skill_invocation(self, user_prompt: str):
        if not self.config.skills_enabled or not user_prompt.startswith("/"):
            return None

        stripped = user_prompt[1:].strip()
        if not stripped:
            return None

        cmd_name, _, remainder = stripped.partition(" ")
        cmd_name = cmd_name.lower()
        cwd = resolve_skill_context_cwd(
            self.config.cwd,
            self.core_engine._context,
        )
        skill = self.skill_registry.find_visible(cmd_name, cwd)
        if skill is None or not skill.frontmatter.user_invocable:
            return None
        return skill, remainder.strip()

    async def _approve_project_skill_shell(self, request) -> bool:
        loop = asyncio.get_running_loop()
        self._skill_permission_future = loop.create_future()
        try:
            if self._skill_permission_queue is None:
                raise RuntimeError("Skill permission queue is not available.")
            await self._skill_permission_queue.put(
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

    @staticmethod
    def _normalize_skill_user_prompt(skill_name: str, skill_args: str) -> str:
        if skill_args:
            return f"Use skill '{skill_name}' with arguments: {skill_args}"
        return f"Use skill '{skill_name}'."

    @staticmethod
    def _strip_transient_skill_messages(state: AgentState) -> None:
        state.messages = strip_skill_context_messages(state.messages)


def create_query_engine(config: Config | None = None) -> QueryEngine:
    return QueryEngine(config=config)
