"""Skill execution for inline and forked skills."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..tools.registry import ToolRegistry
from .loader import SkillLoader
from .models import SkillSource, SkillSpec
from .prompt_processor import PromptProcessor, SkillShellPermissionRequest

if TYPE_CHECKING:
    from ..agent.definitions import AgentDef
    from ..agent.subagent import SubAgent


SKILL_INLINE_SOURCE = "skill_inline"
SKILL_INLINE_META_KEY = "xxcode_skill_context"
SKILL_INLINE_ALLOWED_TOOLS_KEY = "xxcode_skill_allowed_tools"
SKILL_INLINE_DISABLE_SKILL_TOOL_KEY = "xxcode_skill_disable_skill_tool"

EFFORT_THINKING_BUDGETS: dict[str, int] = {
    "quick": 1024,
    "standard": 4096,
}


@dataclass(slots=True)
class SkillExecutionResult:
    mode: str
    prompt: str | None = None
    result_text: str | None = None
    subagent_scope: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class SkillExecutor:
    """Execute skills for the current conversation."""

    def __init__(self, loader: SkillLoader, prompt_processor: PromptProcessor):
        self._loader = loader
        self._prompt_processor = prompt_processor

    async def execute_inline(
        self,
        skill: SkillSpec,
        args: str,
        *,
        session_id: str | None,
        approve_project_shell,
    ) -> SkillExecutionResult:
        loaded = self._loader.load_full_content(skill)
        session_id = session_id or uuid.uuid4().hex[:12]
        prompt = await self._prompt_processor.process(
            loaded,
            args,
            session_id=session_id,
            approve_project_shell=approve_project_shell,
        )
        return SkillExecutionResult(mode="inline", prompt=prompt)

    async def execute(
        self,
        skill: SkillSpec,
        args: str,
        *,
        session_id: str | None,
        approve_project_shell,
        base_registry: ToolRegistry | None = None,
        parent_state: Any = None,
        extra_context: dict[str, Any] | None = None,
    ) -> SkillExecutionResult:
        if skill.frontmatter.context == "fork":
            return await self.execute_fork(
                skill,
                args,
                session_id=session_id,
                approve_project_shell=approve_project_shell,
                base_registry=base_registry,
                parent_state=parent_state,
                extra_context=extra_context,
            )
        return await self.execute_inline(
            skill,
            args,
            session_id=session_id,
            approve_project_shell=approve_project_shell,
        )

    async def execute_fork(
        self,
        skill: SkillSpec,
        args: str,
        *,
        session_id: str | None,
        approve_project_shell,
        base_registry: ToolRegistry | None,
        parent_state: Any = None,
        extra_context: dict[str, Any] | None = None,
    ) -> SkillExecutionResult:
        from ..agent.definitions import AgentDef

        loaded = self._loader.load_full_content(skill)
        session_id = session_id or uuid.uuid4().hex[:12]
        prompt = await self._prompt_processor.process(
            loaded,
            args,
            session_id=session_id,
            approve_project_shell=approve_project_shell,
        )
        prompt = self._apply_fork_effort(prompt, skill.frontmatter.effort)

        subagent_scope = f"subagent:{uuid.uuid4().hex[:12]}"
        filtered_registry = self._build_fork_registry(
            base_registry,
            allowed_tools=skill.frontmatter.allowed_tools,
        )
        thinking_budget_tokens = self._resolve_fork_thinking_budget(
            skill.frontmatter.effort
        )
        definition = AgentDef(
            name=skill.frontmatter.agent or "skill-fork",
            description=f"Forked skill runner for '{skill.canonical_name}'.",
            max_turns=50,
            permission_mode="inherit",
        )
        subagent = self._create_subagent(
            registry=filtered_registry,
            definition=definition,
            parent_state=parent_state,
            model_override=skill.frontmatter.model,
            thinking_budget_tokens=thinking_budget_tokens,
            agent_type=skill.frontmatter.agent or "skill-fork",
            extra_context={
                **(extra_context or {}),
                "_skill_agent_scope": subagent_scope,
            },
        )

        input_tokens = 0
        output_tokens = 0
        try:
            result_text = await subagent.run(prompt)
            usage = getattr(subagent, "tokens_used", (0, 0))
            if isinstance(usage, tuple) and len(usage) == 2:
                input_tokens, output_tokens = usage
        finally:
            persistence = (extra_context or {}).get("_skill_persistence")
            if persistence is not None:
                persistence.clear_for_scope(subagent_scope)

        return SkillExecutionResult(
            mode="fork",
            prompt=prompt,
            result_text=result_text,
            subagent_scope=subagent_scope,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    @staticmethod
    def build_inline_skill_message(
        skill: SkillSpec,
        prompt: str,
        *,
        disable_skill_tool: bool = False,
    ) -> dict:
        source_label = {
            SkillSource.USER: "user skill",
            SkillSource.PROJECT: "project skill",
            SkillSource.BUNDLED: "bundled skill",
        }.get(skill.source, "skill")

        # Strip any existing system-reminder / system-reminder-like tags
        # from the skill prompt to avoid nesting.
        import re
        sanitized = re.sub(
            r"</?system[-_]reminder[^>]*>", "", prompt, flags=re.IGNORECASE,
        ).strip()

        text = (
            "<system-reminder>\n"
            f"Skill '{skill.canonical_name}' ({source_label}) is active for this turn.\n\n"
            f"{sanitized}\n"
            "</system-reminder>"
        )
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "isMeta": True,
            "metadata": {
                SKILL_INLINE_META_KEY: True,
                "source": SKILL_INLINE_SOURCE,
                "skill_name": skill.canonical_name,
                SKILL_INLINE_ALLOWED_TOOLS_KEY: (
                    list(skill.frontmatter.allowed_tools)
                    if skill.frontmatter.allowed_tools is not None
                    else None
                ),
                "xxcode_skill_model": skill.frontmatter.model or None,
                "xxcode_skill_effort": skill.frontmatter.effort,
                SKILL_INLINE_DISABLE_SKILL_TOOL_KEY: disable_skill_tool,
            },
        }

    def _build_fork_registry(
        self,
        base_registry: ToolRegistry | None,
        *,
        allowed_tools: list[str] | None,
    ) -> ToolRegistry:
        if base_registry is None:
            return ToolRegistry()

        if allowed_tools is not None:
            return base_registry.filtered_copy(allow_list=set(allowed_tools))

        # No explicit allow-list — delegate with a conservative read-only pool
        # rather than inheriting the parent's write-capable toolset.
        return base_registry.filtered_copy(read_only_only=True)

    def _create_subagent(
        self,
        *,
        registry: ToolRegistry,
        definition: AgentDef,
        parent_state: Any,
        model_override: str | None,
        thinking_budget_tokens: int | None,
        agent_type: str,
        extra_context: dict[str, Any],
    ) -> SubAgent:
        from ..agent.subagent import SubAgent

        return SubAgent(
            config=self._prompt_processor.config,
            registry=registry,
            definition=definition,
            parent_state=parent_state,
            model_override=model_override,
            thinking_budget_tokens=thinking_budget_tokens,
            agent_type=agent_type,
            extra_context=extra_context,
        )

    @staticmethod
    def _apply_fork_effort(prompt: str, effort: str | int | None) -> str:
        if effort is None:
            return prompt
        if effort == "quick":
            guidance = (
                "Reasoning effort: quick. Prefer the shortest viable path, "
                "keep tool use minimal, and avoid broad exploration."
            )
        elif effort == "standard":
            guidance = (
                "Reasoning effort: standard. Use normal depth, verify key "
                "assumptions, and favor reliable completion over speed."
            )
        else:
            guidance = (
                f"Reasoning effort: {effort}. Use a deliberate number of "
                "steps proportional to this setting when it helps."
            )
        return f"<system-reminder>\n{guidance}\n</system-reminder>\n\n{prompt}"

    @staticmethod
    def _resolve_fork_thinking_budget(effort: str | int | None) -> int | None:
        if effort is None:
            return None
        if isinstance(effort, int):
            return max(0, effort) or None
        return EFFORT_THINKING_BUDGETS.get(effort)


__all__ = [
    "EFFORT_THINKING_BUDGETS",
    "SKILL_INLINE_ALLOWED_TOOLS_KEY",
    "SKILL_INLINE_META_KEY",
    "SKILL_INLINE_SOURCE",
    "SkillExecutionResult",
    "SkillExecutor",
    "SkillShellPermissionRequest",
]
