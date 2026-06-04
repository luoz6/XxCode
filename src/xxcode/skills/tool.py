"""Tool wrapper for model-invoked skills."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..tools import Tool
from .executor import SkillExecutor
from .registry import SkillRegistry


class SkillToolInput(BaseModel):
    skill: str = Field(description="Canonical skill name to invoke.")
    args: str = Field(default="", description="Optional raw argument string for the skill.")


class SkillTool(Tool):
    """Allow the model to activate a registered skill."""

    name = "Skill"
    description = (
        "Invoke a local skill by canonical name. "
        "Use this when a listed skill matches the task better than continuing unaided."
    )
    input_schema = SkillToolInput

    _is_read_only = False
    _is_concurrency_safe = True
    _is_destructive = False

    def __init__(self, registry: SkillRegistry, executor: SkillExecutor):
        self._skill_registry = registry
        self._executor = executor

    async def validate_input(
        self,
        input: SkillToolInput,
        context: dict[str, Any],
    ) -> tuple[bool, str]:
        if not input.skill.strip():
            return False, "skill must not be empty."
        cwd = Path(context.get("cwd") or ".")
        skill = self._skill_registry.find_visible(input.skill, cwd)
        if skill is None:
            return False, (
                f"Skill not found or not visible from the current path: {input.skill}"
            )
        if skill.frontmatter.disable_model_invocation:
            return False, (
                f"Skill '{skill.canonical_name}' cannot be invoked automatically by the model."
            )
        return True, ""

    async def execute(
        self,
        input: SkillToolInput,
        context: dict[str, Any],
    ) -> str:
        cwd = Path(context.get("cwd") or ".")
        skill = self._skill_registry.find_visible(input.skill, cwd)
        if skill is None:
            return f"Skill not found or not visible from the current path: {input.skill}"
        if skill.frontmatter.disable_model_invocation:
            return f"Skill '{skill.canonical_name}' cannot be invoked automatically by the model."

        async def approve_project_shell(request) -> bool:
            requester = context.get("_request_skill_shell_permission")
            if requester is None:
                return False
            return await requester(request)

        execution = await self._executor.execute(
            skill,
            input.args,
            session_id=context.get("_session_id") or uuid.uuid4().hex[:12],
            approve_project_shell=approve_project_shell,
            base_registry=context.get("_registry"),
            parent_state=context.get("parent_state"),
            extra_context=context,
        )

        prompt = execution.prompt or ""
        persistence = context.get("_skill_persistence")
        if persistence is not None and prompt:
            persistence.record_invocation(
                context.get("_skill_agent_scope", "main"),
                skill.canonical_name,
                str(skill.skill_file or skill.directory or ""),
                prompt,
                turn_count=int(getattr(context.get("parent_state"), "turn_count", 0) or 0),
            )

        if execution.mode == "inline":
            pending_messages = context.setdefault("_pending_skill_messages", [])
            pending_messages.append(
                self._executor.build_inline_skill_message(skill, prompt)
            )
            return (
                f"Skill '{skill.canonical_name}' injected into the conversation. "
                "Continue using the new skill context."
            )

        return execution.result_text or f"Skill '{skill.canonical_name}' completed."

    def needs_permission(self, input: BaseModel) -> bool:
        # Skill activation itself is a control-flow change, not a destructive
        # side effect. Project-skill inline shell commands still request
        # approval through the dedicated skill permission path.
        return False

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, SkillToolInput)
        args = f" {input.args}" if input.args else ""
        return f"Skill({input.skill}{args})"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"Skill failed: {content[:150]}"
        return content[:200]


__all__ = ["SkillTool", "SkillToolInput"]
